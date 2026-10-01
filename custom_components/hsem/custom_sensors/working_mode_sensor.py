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
from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.custom_sensors.applier import (
    async_apply_battery_settings,
    async_apply_inverter_power_control,
)
from custom_components.hsem.custom_sensors.applier_emergency_stop import (
    GridChargeEmergencyStopMixin,
)
from custom_components.hsem.custom_sensors.phase_charge_transition import (
    PhaseChargeTransitionMixin,
)
from custom_components.hsem.custom_sensors.recommendation_resolver import (
    resolve_current_recommendation,
)
from custom_components.hsem.custom_sensors.working_mode_attributes import (
    build_working_mode_attributes,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.degraded_mode import hardware_writes_allowed
from custom_components.hsem.utils.inverter_verify import ApplyStatus, CycleApplySummary
from custom_components.hsem.utils.logger import (
    HSEM_LOGGER as _LOGGER,
    log_latched_warning,
)
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_working_mode_sensor_entity_id,
    get_working_mode_sensor_unique_id,
)


class HSEMWorkingModeSensor(
    PhaseChargeTransitionMixin,
    GridChargeEmergencyStopMixin,
    HSEMCoordinatorEntity,
    SensorEntity,
    HSEMEntity,
):
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
    _attr_translation_key = "working_mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [r.value for r in Recommendations]
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        config_entry: ConfigEntry,
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

        self._attr_unique_id = get_working_mode_sensor_unique_id(config_entry.entry_id)
        self.entity_id = get_working_mode_sensor_entity_id()

        # Tracks the latest background update task so it can be cancelled on
        # unload.  Only the most-recent task is retained; prior tasks will
        # have already completed or been replaced.
        self._update_task: asyncio.Task | None = None

        # True while ``_update_task`` is inside ``_async_apply_hardware_writes``
        # actively issuing hardware writes (issue #951). While set, a routine
        # coordinator push must not cancel the task — a full write sequence
        # can outlast the 10s live-power tick, and cancelling mid-sequence can
        # strand the inverter after only the earlier writes landed. The push
        # is coalesced into ``_coordinator_update_pending`` instead.
        self._write_phase_active: bool = False
        # Set when a coordinator push arrives while ``_write_phase_active`` is
        # True. Consumed by ``_on_update_task_done`` to start exactly one
        # follow-up task against the latest coordinator data once the
        # in-flight write sequence finishes, so the newer state is deferred
        # rather than lost.
        self._coordinator_update_pending: bool = False

        # Live phase-aware grid-charge transition tracking (issue #831).
        # Cleared/rearmed per-slot; see _primary_grid_charge_transition_status()
        # in PhaseChargeTransitionMixin.
        self._init_phase_charge_transition_state()

        # Error-mode emergency-stop ownership (issue #840). Survives across
        # cycles so a failed emergency write is retried, not abandoned; never
        # latched from hardware state alone (never claims an
        # externally/manually armed charge).
        self._primary_grid_charge_owned: bool = False

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str | None:
        """Return the working-mode recommendation for the current slot."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.state

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    @property
    @override
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

        attributes = build_working_mode_attributes(
            data,
            cfg,
            live,
            unique_id=self._attr_unique_id,
            config_entry=self._config_entry,
            primary_grid_charge_owned=self._primary_grid_charge_owned,
        )

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

        return dict(sorted({**attributes, **status}.items()))

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Register coordinator listener and run an initial hardware-write pass."""
        await super().async_added_to_hass()
        self._transition_deadline_tasks_enabled = True
        # If the coordinator already has data (from its first cycle in setup),
        # apply hardware settings immediately so the entity is not stale.
        if self.coordinator.data is not None:
            await self._async_apply_hardware_writes(self.coordinator.data)

    @override
    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending background update task before unloading.

        This prevents a stale task from issuing inverter/battery writes after
        the config entry has been unloaded.  A verified but unsettled
        grid-charge transition (issue #831) cannot be left stranded across a
        reload, so its deadline task is cancelled and awaited here too.
        """
        self._transition_deadline_tasks_enabled = False
        deadline_task = self._primary_grid_charge_deadline_task
        self._clear_primary_grid_charge_transition()
        self._cancel_update_task()
        if deadline_task is not None:
            await asyncio.gather(deadline_task, return_exceptions=True)
        await super().async_will_remove_from_hass()

    def _cancel_update_task(self) -> None:
        """Cancel ``_update_task`` if it exists and has not yet completed.

        Cancellation is silent — ``asyncio.CancelledError`` propagates only
        inside the task itself, which guards the hardware-write path, so no
        inverter command can be issued after this point.
        """
        if self._update_task is not None and not self._update_task.done():
            self._update_task.cancel()

    def _on_update_task_done(self, task: asyncio.Task) -> None:
        """Log any unhandled exception from the coordinator-update task.

        Registered as a ``done_callback`` on ``_update_task`` so that
        uncaught exceptions inside ``_async_on_coordinator_update()`` are
        recorded without breaking the task lifecycle.

        Cancelled tasks are ignored because cancellation is expected when the
        entity is unloaded (a routine coordinator push no longer cancels a
        task once it has entered its write phase — see
        ``_handle_coordinator_update``).

        If a coordinator push was coalesced into ``_coordinator_update_pending``
        while this task was writing, start exactly one follow-up task now so
        the deferred state is applied instead of dropped (issue #951).
        """
        if task.cancelled():
            return

        exc = task.exception()
        if exc is not None:
            _LOGGER.error("Unhandled exception in working-mode update task: %s", exc)

        if self._coordinator_update_pending:
            self._coordinator_update_pending = False
            self._start_update_task()

    # ------------------------------------------------------------------
    # Coordinator callback
    # ------------------------------------------------------------------

    @override
    def _handle_coordinator_update(self) -> None:
        """Receive a coordinator push and schedule hardware writes + state flush.

        Cancels any still-pending previous task before creating the new one so
        that only one update is in-flight at a time — but only while that task
        has not yet started issuing hardware writes. Once ``_update_task`` has
        entered its write phase (``_write_phase_active``), a routine replan
        push (e.g. the 10s live-power tick) must not cancel it mid-command —
        earlier writes in the sequence may have already landed on real
        hardware, and cancelling before the working-mode write can strand the
        inverter in the wrong mode (issue #951). The push is coalesced instead
        via ``_coordinator_update_pending`` and applied by a follow-up task
        once the in-flight sequence completes.

        This does not weaken entity unload: ``async_will_remove_from_hass``
        calls ``_cancel_update_task()`` directly and unconditionally.
        """
        if self._write_phase_active:
            self._coordinator_update_pending = True
            return

        # Cancel any still-running task from the previous coordinator cycle.
        self._cancel_update_task()
        self._start_update_task()

    def _start_update_task(self) -> None:
        """Create ``_update_task`` and register its completion callback."""
        task = self.hass.async_create_task(
            self._async_on_coordinator_update(),
            name="hsem_working_mode_update",
        )
        self._update_task = task
        task.add_done_callback(self._on_update_task_done)

    async def _async_on_coordinator_update(self) -> None:
        """Apply hardware writes then write state to HA.

        This method runs asynchronously after every coordinator refresh.
        A ``CancelledError`` is re-raised immediately so that asyncio can
        clean up the task correctly; no hardware write can occur after
        cancellation.  Other exceptions are logged so that hardware-write
        failures are visible in the HA log.
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
        except Exception:
            _LOGGER.error("Hardware-write task failed during coordinator update")

    async def _async_apply_hardware_writes(self, data: CoordinatorData | None) -> None:
        """Perform inverter and battery hardware writes for the current slot.

        Writes are skipped when:
        - ``data`` is ``None``,
        - ``cfg.read_only`` is ``True``, or
        - the degraded mode is ``Error`` (critical entities missing).

        A real-time slot override is applied via :func:`resolve_current_recommendation`
        before issuing the hardware commands.

        ``_write_phase_active`` is set for the duration of this call (issue
        #951) so ``_handle_coordinator_update`` knows a routine coordinator
        push must not cancel the enclosing task once it reaches here — doing
        so mid-sequence can strand the inverter after only the earlier writes
        landed. The flag is cleared via ``finally`` on every exit path,
        including cancellation (e.g. entity unload).

        Args:
            data: The latest :class:`CoordinatorData` snapshot from the coordinator,
                or ``None`` when the coordinator has no data yet.
        """
        if data is None:
            return

        self._write_phase_active = True
        try:
            cfg = data.cfg
            live = data.live

            if cfg is None or live is None:
                self._clear_primary_grid_charge_transition()
                return

            hourly_rec = data.hourly_recommendation

            if hourly_rec is None:
                self._clear_primary_grid_charge_transition()

            # Error-mode emergency-stop ownership release (issue #840).
            self._release_primary_grid_charge_ownership_if_safe(cfg, live)

            # Apply real-time override to the active slot.
            if hourly_rec is not None:
                resolve_current_recommendation(
                    hourly_rec,
                    live,
                    cfg,
                )
                # Sync data.state so the sensor's state property reflects the
                # resolved recommendation (e.g. ev_smart_charging) rather than
                # the raw planner output (e.g. batteries_charge_solar).
                data.state = hourly_rec.recommendation
                _LOGGER.debug(
                    "Current hourly recommendation: state=%s  "
                    "ev_charger_calculated_power=%dW  "
                    "ev_second_charger_calculated_power=%dW  "
                    "ev_total_planned_load_kwh=%.3f  "
                    "ev_planned_load_kwh=%.3f  ev_accounted_load_kwh=%.3f",
                    hourly_rec.recommendation,
                    hourly_rec.ev_charger_calculated_power,
                    hourly_rec.ev_second_charger_calculated_power,
                    hourly_rec.ev_total_planned_load_kwh,
                    hourly_rec.ev_planned_load_kwh,
                    hourly_rec.ev_accounted_load_kwh,
                )

            # Gate hardware writes on read_only and degraded mode.
            writes_safe = hardware_writes_allowed(live.degraded_mode)
            combined_summary = CycleApplySummary()
            log_latched_warning(
                self,
                "degraded_mode_blocked",
                not cfg.read_only and not writes_safe,
                "Hardware writes BLOCKED — degraded mode: %s; missing: %s",
                live.degraded_mode.value,
                live.missing_entities_list,
            )
            if cfg.read_only:
                _LOGGER.debug("Hardware writes SKIPPED — read_only=True")
            elif not writes_safe:
                # Narrow downward-only exception (issue #840) — see
                # GridChargeEmergencyStopMixin for the ownership/retry contract.
                emergency_summary = await self._async_run_error_mode_emergency_stop(
                    self, cfg, live, hourly_rec
                )
                combined_summary.results.extend(emergency_summary.results)
            else:
                inv_summary = await async_apply_inverter_power_control(self, cfg, live)
                combined_summary.results.extend(inv_summary.results)

                # Block battery writes if the inverter write already failed.
                if (
                    inv_summary.overall_status != ApplyStatus.FAILED
                    and hourly_rec is not None
                ):
                    transition_reference_w, transition_timed_out = (
                        self._primary_grid_charge_transition_status(
                            cfg, live, hourly_rec
                        )
                    )
                    bat_summary = await async_apply_battery_settings(
                        self,
                        cfg,
                        live,
                        hourly_rec,
                        data.current_required_battery,
                        wait_mode_reserve_kwh=data.current_wait_mode_reserve,
                        primary_grid_charge_transition_reference_w=transition_reference_w,
                        primary_grid_charge_transition_timed_out=transition_timed_out,
                    )
                    combined_summary.results.extend(bat_summary.results)

            # Persist the apply summary onto the coordinator data so the status
            # sensor and extra_state_attributes can surface it to HA.
            data.apply_summary = combined_summary
        finally:
            self._write_phase_active = False

    # ------------------------------------------------------------------
    # Legacy compatibility
    # ------------------------------------------------------------------

    @override
    async def async_update(self, event: Any | None = None) -> None:
        """Manually request a coordinator refresh.

        Kept for backwards compatibility with any callers that invoke
        ``async_update`` directly (e.g. HA service calls).
        """
        await self.coordinator.async_request_refresh()

    async def async_options_updated(self, config_entry: ConfigEntry) -> None:
        """Handle options update from configuration change.

        Delegates to the coordinator so all entities benefit simultaneously.
        """
        await self.coordinator.async_options_updated()
