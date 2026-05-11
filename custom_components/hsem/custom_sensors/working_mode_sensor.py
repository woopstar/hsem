"""Orchestrator for the HSEM working-mode sensor.

Single responsibility: coordinate the collect → populate → plan → apply →
resolve pipeline and manage the HA entity lifecycle (timers, state tracking,
attributes).

The heavy lifting is fully delegated to the pipeline modules:

- :mod:`state_collector` — reads config entry + HA entity states
- :mod:`hourly_data_populator` — fills prices / Solcast / consumption into slots
- :mod:`~custom_components.hsem.planner.engine` — pure-Python scheduling engine
- :mod:`applier` — hardware writes to inverter / batteries
- :mod:`recommendation_resolver` — real-time override of current-slot decision
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import MATCH_ALL
from homeassistant.helpers.event import (
    async_track_time_change,
    async_track_time_interval,
)

from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
    async_apply_inverter_power_control,
)
from custom_components.hsem.custom_sensors.hourly_data_populator import (
    async_populate_avg_house_consumption,
    async_populate_price_and_solcast,
)
from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.custom_sensors.state_collector import (
    async_collect_live_state,
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.planner import run_planner
from custom_components.hsem.utils.misc import (
    async_logger,
    calculate_recommended_threshold,
    convert_to_float,
    convert_to_int,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.sensornames import (
    get_working_mode_sensor_entity_id,
    get_working_mode_sensor_name,
    get_working_mode_sensor_unique_id,
)


class HSEMWorkingModeSensor(SensorEntity, HSEMEntity):
    """HA sensor entity that orchestrates the HSEM working-mode pipeline.

    The sensor owns:
    - The HA entity lifecycle (``async_added_to_hass``, timers, ``async_write_ha_state``)
    - The update lock preventing concurrent execution
    - The ``extra_state_attributes`` property
    - Coordination of the five pipeline stages per update cycle

    It intentionally contains **no** business logic of its own.
    """

    _attr_icon = "mdi:chart-timeline-variant"
    _attr_has_entity_name = True
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(self, config_entry) -> None:
        super().__init__(config_entry)

        self._config_entry = config_entry
        self._state = None
        self._available = False

        self._attr_unique_id = get_working_mode_sensor_unique_id()
        self.entity_id = get_working_mode_sensor_entity_id()
        self._name = get_working_mode_sensor_name()

        self._tz = None
        self._last_updated = None
        self._next_update = None

        self._update_lock = asyncio.Lock()
        self._timer = None
        self._timer_interval = None

        # Pipeline state
        self._cfg = build_sensor_config(config_entry)
        self._live = None  # LiveState, set each cycle
        self._hourly_recommendations: list[HourlyRecommendation] = []
        self._hourly_recommendation: HourlyRecommendation | None = None
        self._batteries_schedules = []
        self._batteries_schedules_remaining_capacity_needed = 0.0
        self._current_required_battery = 0.0

        # Entity resolution cache
        self._force_working_mode_entity: str | None = None
        self._tracked_entities: set[str] = set()
        self._avg_house_consumption_entity_id_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str | None:
        return self._attr_unique_id

    @property
    def state(self) -> str | None:
        return self._state

    @property
    def should_poll(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return self._available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity state attributes."""
        cfg = self._cfg
        live = self._live

        if live is None or live.missing_entities:
            return {
                "status": "error",
                "description": (
                    "Some of the required input sensors from the config flow is missing "
                    "or not reporting a state yet. Check your configuration and make sure "
                    "input sensors are configured correctly."
                ),
                "missing_input_entities_list": (
                    live.missing_entities_list if live else []
                ),
                "last_updated": self._last_updated,
                "next_update": self._next_update,
                "unique_id": self._attr_unique_id,
            }

        if not self._available:
            return {
                "status": "wait",
                "description": "Waiting for sensor to be available.",
                "last_updated": self._last_updated,
                "next_update": self._next_update,
                "unique_id": self._attr_unique_id,
            }

        extended = {}
        if cfg.extended_attributes:
            extended = {
                "energi_data_service_export_entity": cfg.energi_data_service_export,
                "energi_data_service_import_entity": cfg.energi_data_service_import,
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
                "huawei_solar_batteries_end_of_discharge_soc_entity": cfg.huawei_solar_batteries_end_of_discharge_soc,
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
                "next_update": self._next_update,
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
            "batteries_conversion_loss": cfg.batteries_conversion_loss,
            "batteries_current_capacity": live.battery_current_capacity_kwh,
            "batteries_usable_capacity": live.battery_usable_capacity_kwh,
            "batteries_recommended_min_price_threshold": calculate_recommended_threshold(
                cfg.batteries_purchase_price,
                cfg.batteries_expected_cycles,
                live.battery_usable_capacity_kwh,
                cfg.batteries_conversion_loss,
                live.energi_data_service_import_price,
            ),
            "energi_data_service_export_state": live.energi_data_service_export_price,
            "energi_data_service_import_state": live.energi_data_service_import_price,
            "energi_data_service_export_min_price": cfg.energi_data_service_export_min_price,
            "energi_data_service_update_interval": cfg.energi_data_service_update_interval,
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
            "hourly_recommendation": self._hourly_recommendation,
            "hourly_recommendations": self._hourly_recommendations,
            "house_consumption_energy_weight_14d": cfg.house_consumption_energy_weight_14d,
            "house_consumption_energy_weight_1d": cfg.house_consumption_energy_weight_1d,
            "house_consumption_energy_weight_3d": cfg.house_consumption_energy_weight_3d,
            "house_consumption_energy_weight_7d": cfg.house_consumption_energy_weight_7d,
            "house_consumption_power_state": live.house_consumption_power_w,
            "house_power_includes_ev_charger_power": cfg.house_power_includes_ev_charger_power,
            "batteries_schedules_remaining_capacity_needed": self._batteries_schedules_remaining_capacity_needed,
            "batteries_schedules": self._batteries_schedules,
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
            "last_updated": self._last_updated,
            "net_consumption_with_ev": live.net_consumption_with_ev_w,
            "net_consumption": live.net_consumption_w,
            "solar_production_power_state": live.solar_production_power_w,
            "months_winter": cfg.months_winter,
            "months_summer": cfg.months_summer,
            "batteries_enable_excess_export": cfg.batteries_enable_excess_export,
            "batteries_excess_export_discharge_buffer": cfg.batteries_excess_export_discharge_buffer,
            "batteries_excess_export_price_threshold": cfg.batteries_excess_export_price_threshold,
        }

        status = {"status": "read_only" if cfg.read_only else "ok"}

        return {
            key: value
            for key, value in sorted({**attributes, **extended, **status}.items())
        }

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Handle the sensor being added to Home Assistant."""
        self._tz = ZoneInfo(str(self.hass.config.time_zone))
        await self._async_handle_update(None)
        async_track_time_change(
            self.hass,
            self._async_handle_update,
            hour="*",
            minute=0,
            second=10,
        )
        await super().async_added_to_hass()

    async def async_update(self, event=None) -> None:
        """Manually trigger the sensor update."""
        await self._async_handle_update(event)

    async def async_options_updated(self, config_entry) -> None:
        """Handle options update from configuration change."""
        await self._async_handle_update(None)

    # ------------------------------------------------------------------
    # Update pipeline
    # ------------------------------------------------------------------

    async def _async_handle_update(self, event=None) -> None:
        """Drop concurrent updates; run the update cycle while holding the lock."""
        if self._update_lock.locked():
            await async_logger(
                self,
                "------ Update skipped: a previous update cycle is still running.",
            )
            return
        async with self._update_lock:
            await self._async_run_update_cycle(event)

    async def _async_run_update_cycle(self, event=None) -> None:
        """Execute the full collect → populate → plan → apply → resolve cycle."""
        await async_logger(self, f"------ Updating {self._name} state...")
        now = datetime.now().astimezone(self._tz)

        # 1. Reload config
        self._cfg = build_sensor_config(self._config_entry)
        cfg = self._cfg

        # 2. Collect live HA state
        self._live, self._force_working_mode_entity = await async_collect_live_state(
            self,
            cfg,
            self._force_working_mode_entity,
            self._tracked_entities,
        )
        live = self._live

        # 3. Reset & regenerate recommendation slots
        self._hourly_recommendation = None
        self._hourly_recommendations = self._generate_recommendation_intervals(
            cfg.recommendation_interval_minutes,
            cfg.recommendation_interval_length,
        )

        # 4. Build battery schedules from config
        self._batteries_schedules = build_battery_schedules(cfg)
        self._batteries_schedules.sort(key=lambda x: x.start)

        # 5. Populate consumption averages
        if not await async_populate_avg_house_consumption(
            self,
            self._hourly_recommendations,
            cfg,
            self._avg_house_consumption_entity_id_cache,
        ):
            live.missing_entities = True

        # Update timer based on missing entities
        if live.missing_entities:
            await self._set_update_interval(1)
        else:
            await self._set_update_interval()

        # 6. Short-circuit when forced or missing
        if live.missing_entities and live.force_working_mode_state == "auto":
            self._state = Recommendations.MissingInputEntities.value
            await async_logger(self, "Missing input entities, skipping calculations.")

        elif live.force_working_mode_state != "auto":
            self._state = str(live.force_working_mode_state)
            await async_logger(
                self,
                f"Force working mode is activated. Setting working mode to {live.force_working_mode_state}",
            )

        else:
            # 7. Populate prices and Solcast
            await async_populate_price_and_solcast(
                self, self._hourly_recommendations, cfg, self._tz
            )

            # 8. Run the pure-Python planner engine
            planner_input = self._build_planner_input()
            planner_output = run_planner(planner_input)

            for warning in planner_output.warnings:
                await async_logger(self, f"[planner] {warning}")

            self._apply_planner_output(planner_output)
            self._current_required_battery = planner_output.required_capacity_kwh

            # 9. Find the current time-slot recommendation
            self._hourly_recommendations.sort(key=lambda x: x.start)
            hourly_rec = next(
                (
                    r
                    for r in self._hourly_recommendations
                    if r.start.astimezone(self._tz) <= now < r.end.astimezone(self._tz)
                ),
                None,
            )

            # 10. Apply real-time overrides to current slot
            if hourly_rec is not None:
                resolve_current_recommendation(
                    hourly_rec,
                    live,
                    self._batteries_schedules_remaining_capacity_needed,
                )

            await async_logger(self, f"Current hourly recommendation: {hourly_rec}")

            # 11. Apply hardware settings (inverter + batteries)
            if not cfg.read_only:
                await async_apply_inverter_power_control(self, cfg, live)
                if hourly_rec is not None:
                    await async_apply_battery_settings(
                        self, cfg, live, hourly_rec, self._current_required_battery
                    )

            # 12. Store current recommendation
            if hourly_rec:
                self._hourly_recommendation = hourly_rec
                self._state = hourly_rec.recommendation
            else:
                self._state = None

        # Final sort
        self._hourly_recommendations.sort(key=lambda x: x.start)
        self._last_updated = now.isoformat()
        self._available = True

        await async_logger(self, f"------ Completed updating {self._name} state...")
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Planner bridge helpers (unchanged from prior PR)
    # ------------------------------------------------------------------

    def _build_planner_input(self) -> PlannerInput:
        """Assemble a :class:`PlannerInput` from the current HA-fetched state."""
        cfg = self._cfg
        now = datetime.now().astimezone(self._tz)

        seen_hours: set[int] = set()
        consumption_averages: list[HourlyConsumptionAverage] = []
        price_points: list[PricePoint] = []
        solcast_slots: list[SolcastSlot] = []

        slots_per_hour = 60.0 / cfg.recommendation_interval_minutes
        eds_share = (
            cfg.energi_data_service_update_interval
            / cfg.recommendation_interval_minutes
        )

        for rec in self._hourly_recommendations:
            h = rec.start.hour
            if h in seen_hours:
                continue
            seen_hours.add(h)

            consumption_averages.append(
                HourlyConsumptionAverage(
                    hour=h,
                    avg_1d=round(rec.avg_house_consumption_1d * slots_per_hour, 3),
                    avg_3d=round(rec.avg_house_consumption_3d * slots_per_hour, 3),
                    avg_7d=round(rec.avg_house_consumption_7d * slots_per_hour, 3),
                    avg_14d=round(rec.avg_house_consumption_14d * slots_per_hour, 3),
                )
            )
            price_points.append(
                PricePoint(
                    hour=h,
                    import_price=round(rec.import_price * eds_share, 5),
                    export_price=round(rec.export_price * eds_share, 5),
                )
            )
            solcast_slots.append(
                SolcastSlot(
                    hour=h,
                    pv_estimate=round(rec.solcast_pv_estimate * slots_per_hour, 3),
                )
            )

        battery_schedules = [
            BatteryScheduleInput(
                enabled=s.enabled,
                start=s.start,
                end=s.end,
                min_price_difference=s.min_price_difference_required,
            )
            for s in self._batteries_schedules
        ]

        live = self._live
        return PlannerInput(
            now_iso=now.isoformat(),
            interval_minutes=cfg.recommendation_interval_minutes,
            interval_length_hours=cfg.recommendation_interval_length,
            battery_soc_pct=convert_to_float(live.huawei_batteries_soc_pct),
            battery_rated_capacity_kwh=(
                convert_to_float(live.huawei_batteries_rated_capacity_wh) or 0.0
            )
            / 1000.0,
            battery_end_of_discharge_soc_pct=convert_to_float(
                live.huawei_batteries_end_of_discharge_soc_pct or 5.0
            ),
            battery_max_charge_power_w=convert_to_float(
                live.huawei_batteries_max_charge_power_w
            ),
            battery_max_discharge_power_w=convert_to_float(
                live.huawei_batteries_max_discharge_power_w
            )
            or None,
            battery_conversion_loss_pct=convert_to_float(cfg.batteries_conversion_loss),
            battery_purchase_price=convert_to_float(cfg.batteries_purchase_price),
            battery_expected_cycles=convert_to_int(cfg.batteries_expected_cycles),
            weight_1d=convert_to_int(cfg.house_consumption_energy_weight_1d),
            weight_3d=convert_to_int(cfg.house_consumption_energy_weight_3d),
            weight_7d=convert_to_int(cfg.house_consumption_energy_weight_7d),
            weight_14d=convert_to_int(cfg.house_consumption_energy_weight_14d),
            consumption_averages=consumption_averages,
            price_points=price_points,
            solcast_slots=solcast_slots,
            battery_schedules=battery_schedules,
            excess_export_enabled=bool(cfg.batteries_enable_excess_export),
            excess_export_discharge_buffer_pct=convert_to_float(
                cfg.batteries_excess_export_discharge_buffer
            ),
            excess_export_price_threshold=convert_to_float(
                cfg.batteries_excess_export_price_threshold
            ),
            export_min_price=convert_to_float(cfg.energi_data_service_export_min_price),
            months_winter=list(cfg.months_winter or []),
            house_power_includes_ev=bool(cfg.house_power_includes_ev_charger_power),
            is_read_only=bool(cfg.read_only),
        )

    def _apply_planner_output(self, output) -> None:
        """Write :class:`PlannerOutput` decisions back into ``self._hourly_recommendations``."""
        slot_by_start = {s.start: s for s in output.slots}

        for rec in self._hourly_recommendations:
            slot = slot_by_start.get(rec.start)
            if slot is None:
                continue
            rec.recommendation = slot.recommendation
            rec.batteries_charged = slot.batteries_charged
            rec.estimated_net_consumption = slot.estimated_net_consumption
            rec.estimated_cost = slot.estimated_cost
            rec.estimated_battery_capacity = slot.estimated_battery_capacity
            rec.estimated_battery_soc = slot.estimated_battery_soc

        self._batteries_schedules_remaining_capacity_needed = sum(
            s.needed_batteries_capacity for s in self._batteries_schedules if s.enabled
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _generate_recommendation_intervals(
        self, interval_minutes: int, total_hours: int
    ) -> list[HourlyRecommendation]:
        """Generate empty recommendation slots from midnight for ``total_hours`` hours."""
        now = datetime.now().astimezone(self._tz)
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0)
        steps = int((total_hours * 60) / interval_minutes)

        intervals = []
        for i in range(steps):
            t_start = start_time + timedelta(minutes=i * interval_minutes)
            t_end = t_start + timedelta(minutes=interval_minutes)
            intervals.append(
                HourlyRecommendation(
                    avg_house_consumption=0.0,
                    avg_house_consumption_1d=0.0,
                    avg_house_consumption_3d=0.0,
                    avg_house_consumption_7d=0.0,
                    avg_house_consumption_14d=0.0,
                    batteries_charged=0.0,
                    end=t_end,
                    estimated_battery_capacity=0.0,
                    estimated_battery_soc=0,
                    estimated_cost=0.0,
                    estimated_net_consumption=0.0,
                    export_price=0.0,
                    import_price=0.0,
                    recommendation=None,
                    solcast_pv_estimate=0.0,
                    start=t_start,
                )
            )
        return intervals

    async def _set_update_interval(self, override_interval=None) -> None:
        """Register or re-register the periodic timer."""
        cfg = self._cfg
        interval = timedelta(
            minutes=override_interval if override_interval else cfg.update_interval
        )
        if self._timer_interval != interval:
            self._timer_interval = interval
            await self._async_register_timer(interval)
        self._next_update = (datetime.now().astimezone(self._tz) + interval).isoformat()

    async def _async_register_timer(self, interval: timedelta) -> None:
        """Cancel any existing timer and register a new periodic one."""
        if self._timer:
            self._timer()
            self._timer = None
        self._timer = async_track_time_interval(
            self.hass, self._async_handle_update, interval
        )
        await async_logger(self, f"Updating HSEM with interval: {interval}.")
