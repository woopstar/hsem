"""Working-mode sensor for HSEM.

This entity subscribes to :class:`~custom_components.hsem.coordinator.HSEMDataUpdateCoordinator`
and is responsible for:

- Exposing the current working-mode recommendation as HA sensor state.
- Performing hardware writes (inverter + battery commands) after each coordinator
  cycle, gated by ``read_only`` and degraded-mode checks.
- Applying real-time slot overrides via :mod:`recommendation_resolver`.
- Exposing all planning data as ``extra_state_attributes``.

The heavy pipeline work (collect → populate → plan) has moved to the
coordinator.  This entity only reacts to coordinator pushes.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import MATCH_ALL

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
    async_apply_inverter_power_control,
)
from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed
from custom_components.hsem.utils.inverter_verify import ApplyStatus, CycleApplySummary
from custom_components.hsem.utils.logger import async_logger
from custom_components.hsem.utils.misc import calculate_recommended_threshold
from custom_components.hsem.utils.sensornames import (
    get_working_mode_sensor_entity_id,
    get_working_mode_sensor_name,
    get_working_mode_sensor_unique_id,
)


class HSEMWorkingModeSensor(HSEMCoordinatorEntity, SensorEntity, HSEMEntity):
    """HA sensor entity for the HSEM working-mode recommendation.

    Subscribes to :class:`HSEMDataUpdateCoordinator` for shared state and
    performs hardware writes after each cycle.

    State
    -----
    The ``state`` property reflects the working-mode recommendation string
    for the current planning slot, or a sentinel value such as
    ``"missing_input_entities"`` when required sensors are unavailable.

    Attributes
    ----------
    ``extra_state_attributes`` returns the full planning snapshot including
    battery schedules, price data, EV state, and Solcast estimates.
    """

    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        config_entry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the working-mode sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_working_mode_sensor_unique_id()
        self.entity_id = get_working_mode_sensor_entity_id()
        self._name = get_working_mode_sensor_name()

        # Tracks the latest background update task so it can be cancelled on
        # unload.  Only the most-recent task is retained; prior tasks will
        # have already completed or been replaced.
        self._update_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Return the display name."""
        return self._name

    @property
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    def state(self) -> str | None:
        """Return the working-mode recommendation for the current slot."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.state

    @property
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity state attributes."""
        data: CoordinatorData | None = self.coordinator.data

        if data is None or data.live is None:
            return {
                "status": "wait",
                "description": "Waiting for coordinator to complete first cycle.",
                "last_updated": None,
                "next_update": None,
                "unique_id": self._attr_unique_id,
            }

        cfg = data.cfg
        live = data.live

        # Guard against a partially-initialised coordinator snapshot where cfg
        # was not yet populated (should not happen after first cycle but
        # prevents AttributeError on None during startup race).
        if cfg is None:
            return {
                "status": "wait",
                "description": "Waiting for coordinator configuration to be loaded.",
                "last_updated": None,
                "next_update": None,
                "unique_id": self._attr_unique_id,
            }

        if live.missing_entities:
            return {
                "status": "error",
                "description": (
                    "Some of the required input sensors from the config flow is missing "
                    "or not reporting a state yet. Check your configuration and make sure "
                    "input sensors are configured correctly."
                ),
                "missing_input_entities_list": live.missing_entities_list,
                "last_updated": data.last_updated,
                "next_update": data.next_update,
                "unique_id": self._attr_unique_id,
            }

        extended = {}
        if cfg.extended_attributes:
            extended = {
                "import_electricity_price_sensor_entity": cfg.import_electricity_price_sensor,
                "export_electricity_price_sensor_entity": cfg.export_electricity_price_sensor,
                "ev_charger_power_entity": cfg.ev.power_entity,
                "ev_charger_status_entity": cfg.ev.status_entity,
                "ev_soc_entity": cfg.ev.soc_entity,
                "ev_soc_target_entity": cfg.ev.soc_target_entity,
                "ev_connected_entity": cfg.ev.connected_entity,
                "ev_second_charger_power_entity": cfg.ev_second.power_entity,
                "ev_second_charger_status_entity": cfg.ev_second.status_entity,
                "ev_second_soc_entity": cfg.ev_second.soc_entity,
                "ev_second_soc_target_entity": cfg.ev_second.soc_target_entity,
                "ev_second_connected_entity": cfg.ev_second.connected_entity,
                "force_working_mode_entity": live.force_working_mode,
                "house_consumption_power_entity": cfg.house_consumption_power,
                "hsem_huawei_solar_batteries_end_of_discharge_soc_entity": cfg.huawei_solar_batteries_end_of_discharge_soc,
                "huawei_solar_batteries_grid_charge_cutoff_soc_entity": cfg.huawei_solar_batteries_grid_charge_cutoff_soc,
                "huawei_solar_batteries_maximum_charging_power_entity": cfg.huawei_solar_batteries_maximum_charging_power,
                "huawei_solar_batteries_maximum_discharging_power_entity": cfg.huawei_solar_batteries_maximum_discharging_power,
                "huawei_solar_batteries_rated_capacity_max_entity": cfg.huawei_solar_batteries_rated_capacity,
                "huawei_solar_batteries_state_of_capacity_entity": cfg.huawei_solar_batteries_state_of_capacity,
                "huawei_solar_batteries_tou_charging_and_discharging_periods_entity": cfg.huawei_solar_batteries_tou_charging_and_discharging_periods,
                "huawei_solar_batteries_working_mode_entity": cfg.huawei_solar_batteries_working_mode,
                "huawei_solar_device_id_batteries_id": cfg.huawei_solar_device_id_batteries,
                "huawei_solar_device_id_inverter_1_id": cfg.huawei_solar_device_id_inverter_1,
                "huawei_solar_device_id_inverter_2_id": cfg.huawei_solar_device_id_inverter_2,
                "huawei_solar_inverter_active_power_control_state_entity": cfg.huawei_solar_inverter_active_power_control,
                "next_update": data.next_update,
                "read_only": cfg.read_only,
                "solar_production_power_entity": cfg.solar_production_power,
                "solcast_pv_forecast_forecast_today_entity": cfg.solcast_pv_forecast_forecast_today,
                "solcast_pv_forecast_forecast_tomorrow_entity": cfg.solcast_pv_forecast_forecast_tomorrow,
                "unique_id": self._attr_unique_id,
                "update_interval": cfg.update_interval,
                "recommendation_interval_minutes": cfg.recommendation_interval_minutes,
                "recommendation_interval_length": cfg.recommendation_interval_length,
            }

        attributes = {
            "batteries_current_capacity": live.battery_current_capacity_kwh,
            "batteries_usable_capacity": live.battery_usable_capacity_kwh,
            "batteries_recommended_min_price_threshold": calculate_recommended_threshold(
                purchase_price=cfg.batteries_purchase_price,
                expected_cycles=cfg.batteries_expected_cycles,
                usable_capacity=live.battery_usable_capacity_kwh,
                capacity_loss_pct=cfg.batteries_capacity_loss_pct,
                charge_efficiency_pct=cfg.batteries_charge_efficiency,
                discharge_efficiency_pct=cfg.batteries_discharge_efficiency,
            ),
            "batteries_capacity_loss_pct": cfg.batteries_capacity_loss_pct,
            "export_electricity_price_state": live.export_electricity_price,
            "import_electricity_price_state": live.import_electricity_price,
            "export_electricity_min_price": cfg.export_electricity_min_price,
            "electricity_price_update_interval": cfg.electricity_price_update_interval,
            "ev_charger_power_state": live.ev.power_w,
            "ev_charger_status_state": live.ev.is_charging,
            "ev_soc_state": live.ev.soc_pct,
            "ev_soc_target_state": live.ev.soc_target_pct,
            "ev_connected_state": live.ev.is_connected,
            "ev_allow_charge_past_target_soc": cfg.ev.allow_charge_past_target_soc,
            "ev_charger_max_discharge_power_state": live.ev.max_discharge_power_w,
            "ev_charger_force_max_discharge_power": live.ev.force_max_discharge_power,
            "ev_second_enabled": cfg.ev_second_enabled,
            "ev_second_charger_power_state": live.ev_second.power_w,
            "ev_second_charger_status_state": live.ev_second.is_charging,
            "ev_second_soc_state": live.ev_second.soc_pct,
            "ev_second_soc_target_state": live.ev_second.soc_target_pct,
            "ev_second_connected_state": live.ev_second.is_connected,
            "ev_second_allow_charge_past_target_soc": cfg.ev_second.allow_charge_past_target_soc,
            "ev_second_charger_max_discharge_power_state": live.ev_second.max_discharge_power_w,
            "ev_second_charger_force_max_discharge_power": live.ev_second.force_max_discharge_power,
            "force_working_mode_state": live.force_working_mode_state,
            "hourly_recommendation": data.hourly_recommendation,
            "hourly_recommendations": data.hourly_recommendations,
            "house_consumption_energy_weight_14d": cfg.house_consumption_energy_weight_14d,
            "house_consumption_energy_weight_1d": cfg.house_consumption_energy_weight_1d,
            "house_consumption_energy_weight_3d": cfg.house_consumption_energy_weight_3d,
            "house_consumption_energy_weight_7d": cfg.house_consumption_energy_weight_7d,
            "house_consumption_power_state": live.house_consumption_power_w,
            "house_power_includes_ev_charger_power": cfg.house_power_includes_ev_charger_power,
            "batteries_schedules_remaining_capacity_needed": data.batteries_schedules_remaining_capacity_needed,
            "batteries_schedules": data.batteries_schedules,
            "huawei_solar_batteries_charging_cutoff_capacity_state": live.huawei_batteries_charging_cutoff_capacity_pct,
            "huawei_solar_batteries_grid_charge_cutoff_soc_state": live.huawei_batteries_grid_charge_cutoff_soc_pct,
            "huawei_solar_batteries_maximum_charging_power_state": live.huawei_batteries_max_charge_power_w,
            "huawei_solar_batteries_maximum_discharging_power_state": live.huawei_batteries_max_discharge_power_w,
            "huawei_solar_batteries_rated_capacity_max_state": live.huawei_batteries_rated_capacity_wh,
            "huawei_solar_batteries_rated_capacity_min_state": live.battery_rated_capacity_min_kwh,
            "huawei_solar_batteries_state_of_capacity_state": live.huawei_batteries_soc_pct,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": live.tou_periods.periods,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_state": live.tou_periods.raw_state,
            "huawei_solar_batteries_working_mode_state": live.huawei_batteries_working_mode,
            "huawei_solar_inverter_active_power_control_state_state": live.huawei_inverter_active_power_control,
            "huawei_solar_batteries_excess_pv_energy_use_in_tou_state": live.huawei_batteries_excess_pv_use_in_tou,
            "solcast_pv_forecast_forecast_likelihood": cfg.solcast_pv_forecast_forecast_likelihood,
            "last_updated": data.last_updated,
            "net_consumption_with_ev": live.net_consumption_with_ev_w,
            "net_consumption": live.net_consumption_w,
            "solar_production_power_state": live.solar_production_power_w,
            "months_winter": cfg.months_winter,
            "months_summer": cfg.months_summer,
            "batteries_enable_excess_export": cfg.batteries_enable_excess_export,
            "batteries_excess_export_discharge_buffer": cfg.batteries_excess_export_discharge_buffer,
        }

        apply_summary = data.apply_summary
        status = {
            "status": "read_only" if cfg.read_only else "ok",
            "degraded_mode": live.degraded_mode.value,
            "hardware_writes_blocked": not hardware_writes_allowed(live.degraded_mode),
            "apply_status": (
                apply_summary.overall_status.value if apply_summary else None
            ),
            "apply_failed_entities": (
                apply_summary.failed_entities if apply_summary else []
            ),
            "data_quality": data.data_quality.as_dict(),
        }

        return {
            key: value
            for key, value in sorted({**attributes, **extended, **status}.items())
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Register coordinator listener and run an initial hardware-write pass."""
        await super().async_added_to_hass()
        # If the coordinator already has data (from its first cycle in setup),
        # apply hardware settings immediately so the entity is not stale.
        if self.coordinator.data is not None:
            await self._async_apply_hardware_writes(self.coordinator.data)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending background update task before unloading.

        This prevents a stale task from issuing inverter/battery writes after
        the config entry has been unloaded.
        """
        self._cancel_update_task()
        await super().async_will_remove_from_hass()

    def _cancel_update_task(self) -> None:
        """Cancel ``_update_task`` if it exists and has not yet completed.

        Cancellation is silent — ``asyncio.CancelledError`` propagates only
        inside the task itself, which guards the hardware-write path, so no
        inverter command can be issued after this point.
        """
        if self._update_task is not None and not self._update_task.done():
            self._update_task.cancel()

    # ------------------------------------------------------------------
    # Coordinator callback
    # ------------------------------------------------------------------

    def _handle_coordinator_update(self) -> None:
        """Receive a coordinator push and schedule hardware writes + state flush.

        Cancels any still-pending previous task before creating the new one so
        that only one update is in-flight at a time.  The task reference is
        stored on ``_update_task`` so it can be cancelled on unload.
        """
        # Cancel any still-running task from the previous coordinator cycle.
        self._cancel_update_task()
        self._update_task = self.hass.async_create_task(
            self._async_on_coordinator_update(),
            name="hsem_working_mode_update",
        )

    async def _async_on_coordinator_update(self) -> None:
        """Apply hardware writes then write state to HA.

        This method runs asynchronously after every coordinator refresh.
        A ``CancelledError`` is re-raised immediately so that asyncio can
        clean up the task correctly; no hardware write can occur after
        cancellation.
        """
        try:
            data = self.coordinator.data
            if data is None:
                return

            await self._async_apply_hardware_writes(data)
            self.async_write_ha_state()
        except asyncio.CancelledError:
            # Task was cancelled (entity unloaded) — propagate cleanly.
            raise

    async def _async_apply_hardware_writes(self, data: CoordinatorData) -> None:
        """Perform inverter and battery hardware writes for the current slot.

        Writes are skipped when:
        - ``cfg.read_only`` is ``True``, or
        - the degraded mode is ``Error`` (critical entities missing).

        A real-time slot override is applied via :func:`resolve_current_recommendation`
        before issuing the hardware commands.

        Args:
            data: The latest :class:`CoordinatorData` snapshot from the coordinator.
        """
        cfg = data.cfg
        live = data.live

        if cfg is None or live is None:
            return

        hourly_rec = data.hourly_recommendation

        # Apply real-time override to the active slot.
        if hourly_rec is not None:
            resolve_current_recommendation(
                hourly_rec,
                live,
                data.batteries_schedules_remaining_capacity_needed,
            )
            # Sync data.state so the sensor's state property reflects the
            # resolved recommendation (e.g. ev_smart_charging) rather than
            # the raw planner output (e.g. batteries_charge_solar).
            data.state = hourly_rec.recommendation
            await async_logger(self, f"Current hourly recommendation: {hourly_rec}")

        # Gate hardware writes on read_only and degraded mode.
        writes_safe = hardware_writes_allowed(live.degraded_mode)
        combined_summary = CycleApplySummary()
        if cfg.read_only:
            await async_logger(
                self,
                "Hardware writes SKIPPED — read_only=True",
                "warning",
            )
        elif not writes_safe:
            await async_logger(
                self,
                f"Hardware writes BLOCKED — degraded mode: {live.degraded_mode.value}. Missing: {live.missing_entities_list}",
                "warning",
            )
        else:
            inv_summary = await async_apply_inverter_power_control(self, cfg, live)
            combined_summary.results.extend(inv_summary.results)

            # Block battery writes if the inverter write already failed.
            if (
                inv_summary.overall_status != ApplyStatus.FAILED
                and hourly_rec is not None
            ):
                bat_summary = await async_apply_battery_settings(
                    self,
                    cfg,
                    live,
                    hourly_rec,
                    data.current_required_battery,
                )
                combined_summary.results.extend(bat_summary.results)

        # Persist the apply summary onto the coordinator data so the status
        # sensor and extra_state_attributes can surface it to HA.
        data.apply_summary = combined_summary

    # ------------------------------------------------------------------
    # Legacy compatibility
    # ------------------------------------------------------------------

    async def async_update(self, event=None) -> None:
        """Manually request a coordinator refresh.

        Kept for backwards compatibility with any callers that invoke
        ``async_update`` directly (e.g. HA service calls).
        """
        await self.coordinator.async_request_refresh()

    async def async_options_updated(self, config_entry) -> None:
        """Handle options update from configuration change.

        Delegates to the coordinator so all entities benefit simultaneously.
        """
        await self.coordinator.async_options_updated()
