"""Live phase-aware grid-charge transition safety mixin (issue #831).

Extracted from ``working_mode_sensor.py`` to satisfy the repository's 30 KB /
1000-line file limit. Provides the feedback-free floor's transition
tracking and the 45-second fail-closed deadline for
:class:`~custom_sensors.working_mode_sensor.HSEMWorkingModeSensor`.

Design
------
The live per-phase safety limiter (``phase_charge_limiter.py``) normally
removes the *live* battery-power reading from the phase snapshot before
computing headroom. During a verified downward grid-charge cap change, that
live reading may lag behind the just-verified command for several seconds
while the inverter physically ramps down. Using the stale-low reading
would let the limiter believe more headroom exists than is physically
true.

:class:`PhaseChargeTransitionMixin` tracks exactly one such transition at a
time, scoped to the recommendation slot that armed it:

- While active, it hands the *previous* (higher) cap to the limiter as
  ``primary_grid_charge_transition_reference_w`` instead of trusting the
  live battery-power echo.
- It clears itself as soon as independent telemetry (the verified cap
  read-back **and** the live battery-power reading) both agree with the
  new, lower target.
- If neither settles within 45 seconds, it fails closed: the next write
  pass commands 0 W for the remainder of the slot, and an entity-owned
  deadline task forces that write pass even if no new coordinator cycle or
  state-change event would otherwise trigger one.
- The transition and its deadline task are strictly slot-scoped and are
  cancelled/cleared on unload, so a reload can never leave one stranded.
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

from custom_components.hsem.utils.datetime_utils import utc_key
from custom_components.hsem.utils.inverter_verify import ApplyStatus, CycleApplySummary
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.recommendations import Recommendations

if TYPE_CHECKING:
    from homeassistant.helpers.entity import Entity

    from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
    from custom_components.hsem.models.hourly_recommendation import (
        HourlyRecommendation,
    )
    from custom_components.hsem.models.live_state import LiveState
    from custom_components.hsem.models.sensor_config import SensorConfig

    _Base = Entity
else:
    _Base = object

#: Maximum time (seconds) a verified downward Huawei grid-charge cap change
#: may suppress the raw phase-power feedback before failing closed to 0 W
#: for the remainder of the slot (issue #831).
PRIMARY_GRID_CHARGE_TRANSITION_MAX_SECONDS = 45.0

#: Tolerance (W) for deciding that the live battery-power echo has caught
#: up with a verified lower grid-charge cap.
_PRIMARY_GRID_CHARGE_SETTLED_TOLERANCE_W = 300.0


@dataclass(frozen=True)
class PrimaryGridChargeTransition:
    """One verified downward Huawei grid-charge cap whose physical settlement may still lag.

    The verified writable cap is authoritative outside a known downward
    transition. While a just-verified lower cap is still taking effect, the
    live battery-power echo may remain at the previous, higher command. The
    caller owns this short-lived transition state and clears it only once
    independent telemetry confirms the lower command has physically settled
    (or the 45-second deadline expires, whichever comes first).
    """

    previous_limit_w: float
    target_limit_w: float
    slot_start: datetime
    slot_end: datetime
    expires_at_monotonic: float


class PhaseChargeTransitionMixin(_Base):
    """Transition tracking + 45s fail-closed deadline for grid-charge caps.

    Mixed into :class:`~custom_sensors.working_mode_sensor.HSEMWorkingModeSensor`.
    Expects the host class to provide ``hass``, ``coordinator``,
    ``async_write_ha_state()``, and ``_async_apply_hardware_writes()`` (all
    supplied by the HA entity base classes / the sensor itself), plus the
    per-instance state initialised in
    :meth:`_init_phase_charge_transition_state`.
    """

    hass: Any
    coordinator: HSEMDataUpdateCoordinator

    def _init_phase_charge_transition_state(self) -> None:
        """Initialise transition-tracking fields. Call once from ``__init__``."""
        self._transition_deadline_tasks_enabled = False
        self._primary_grid_charge_transition: PrimaryGridChargeTransition | None = None
        self._primary_grid_charge_timed_out_slot: tuple[datetime, datetime] | None = (
            None
        )
        self._primary_grid_charge_deadline_task: asyncio.Task[None] | None = None

    async def _async_apply_hardware_writes(self, data: Any) -> None:
        """Provided by :class:`HSEMWorkingModeSensor`; declared for typing only."""
        raise NotImplementedError

    def _clear_primary_grid_charge_transition(self) -> None:
        """Clear transition authority and its fail-closed timeout latch."""
        task = self._primary_grid_charge_deadline_task
        self._primary_grid_charge_deadline_task = None
        if task is not None and not task.done():
            try:
                current_task = asyncio.current_task()
            except RuntimeError:
                current_task = None
            if task is not current_task:
                task.cancel()
        self._primary_grid_charge_transition = None
        self._primary_grid_charge_timed_out_slot = None

    @staticmethod
    def _primary_grid_charge_slot(
        rec: HourlyRecommendation,
    ) -> tuple[datetime, datetime]:
        """Return one timezone-stable identity for a recommendation slot."""
        return utc_key(rec.start), utc_key(rec.end)

    def _schedule_primary_grid_charge_deadline(
        self,
        transition: PrimaryGridChargeTransition,
    ) -> None:
        """Arm one task for the Huawei physical-settlement deadline."""
        if not self._transition_deadline_tasks_enabled:
            return
        existing = self._primary_grid_charge_deadline_task
        self._primary_grid_charge_deadline_task = None
        if existing is not None and not existing.done():
            existing.cancel()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Pure synchronous unit tests have no event loop. Runtime writes
            # always execute on Home Assistant's loop.
            return
        self._primary_grid_charge_deadline_task = self.hass.async_create_task(
            self._async_primary_grid_charge_deadline(transition),
            name="hsem_primary_grid_charge_deadline",
        )

    async def _async_primary_grid_charge_deadline(
        self,
        transition: PrimaryGridChargeTransition,
    ) -> None:
        """Force a fresh hardware-write pass at the deadline even if telemetry is silent.

        No new coordinator push is required: an unchanged snapshot at the
        deadline is exactly the condition that must fail closed. The next
        write pass re-evaluates ``_primary_grid_charge_transition_status``,
        which turns the expired transition into a 0 W command.
        """
        try:
            await asyncio.sleep(max(transition.expires_at_monotonic - monotonic(), 0.0))
            if (
                not self._transition_deadline_tasks_enabled
                or self._primary_grid_charge_transition is not transition
            ):
                return
            data = self.coordinator.data
            if data is not None:
                await self._async_apply_hardware_writes(data)
                self.async_write_ha_state()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- background safety task
            _LOGGER.exception("Huawei phase-transition deadline task failed")
        finally:
            if self._primary_grid_charge_deadline_task is asyncio.current_task():
                self._primary_grid_charge_deadline_task = None

    def _primary_grid_charge_transition_status(
        self,
        cfg: SensorConfig,
        live: LiveState,
        rec: HourlyRecommendation,
    ) -> tuple[float | None, bool]:
        """Return old-cap authority or a slot-scoped fail-closed signal.

        Returns ``(transition_reference_w, timed_out)``. ``transition_reference_w``
        is the pre-write cap to pass into
        :func:`~custom_sensors.phase_charge_limiter.build_phase_aware_charge_commands`
        while a verified downward transition has not yet settled, or ``None``
        when no transition is active. ``timed_out`` is ``True`` once the
        45-second deadline has expired without settlement, for the remainder
        of this slot.
        """
        slot = self._primary_grid_charge_slot(rec)
        if (
            rec.recommendation != Recommendations.BatteriesChargeGrid.value
            or not cfg.phase_aware_charging_enabled
        ):
            self._clear_primary_grid_charge_transition()
            return None, False

        transition = self._primary_grid_charge_transition
        timed_out_slot = self._primary_grid_charge_timed_out_slot
        if transition is not None and (
            transition.slot_start != slot[0] or transition.slot_end != slot[1]
        ):
            self._clear_primary_grid_charge_transition()
            transition = None
            timed_out_slot = None
        elif timed_out_slot is not None and timed_out_slot != slot:
            self._primary_grid_charge_timed_out_slot = None
            timed_out_slot = None

        if timed_out_slot == slot:
            return None, True
        if transition is None:
            return None, False

        current_limit_w = live.huawei_batteries_grid_charge_max_power_w
        battery_power_w = live.huawei_batteries_charge_discharge_power_w
        settled = (
            current_limit_w is not None
            and math.isfinite(current_limit_w)
            and abs(current_limit_w - transition.target_limit_w) <= 1.0
            and battery_power_w is not None
            and math.isfinite(battery_power_w)
            and battery_power_w
            <= transition.target_limit_w + _PRIMARY_GRID_CHARGE_SETTLED_TOLERANCE_W
        )
        if settled:
            self._clear_primary_grid_charge_transition()
            return None, False

        if monotonic() >= transition.expires_at_monotonic:
            self._primary_grid_charge_timed_out_slot = slot
            return None, True
        return transition.previous_limit_w, False

    def _record_verified_primary_grid_charge_transition(
        self,
        cfg: SensorConfig,
        live: LiveState,
        rec: HourlyRecommendation,
        target_limit_w: float,
        summary: CycleApplySummary,
    ) -> None:
        """Remember a verified downward cap until physical telemetry settles."""
        charge_entity = cfg.huawei_solar_batteries_grid_charge_maximum_power
        previous_live_limit_w = live.huawei_batteries_grid_charge_max_power_w
        if (
            not cfg.phase_aware_charging_enabled
            or rec.recommendation != Recommendations.BatteriesChargeGrid.value
            or charge_entity is None
            or not math.isfinite(target_limit_w)
            or previous_live_limit_w is None
            or not math.isfinite(previous_live_limit_w)
        ):
            return

        target_limit_w = max(target_limit_w, 0.0)
        verified = any(
            result.entity_id == charge_entity
            and result.status in {ApplyStatus.OK, ApplyStatus.SKIPPED}
            and isinstance(result.desired, int | float)
            and math.isfinite(float(result.desired))
            and abs(float(result.desired) - target_limit_w) <= 1.0
            and isinstance(result.actual, int | float)
            and math.isfinite(float(result.actual))
            and abs(float(result.actual) - target_limit_w) <= 1.0
            for result in summary.results
        )
        if not verified:
            return

        slot = self._primary_grid_charge_slot(rec)
        existing = self._primary_grid_charge_transition
        existing_same_slot = (
            existing is not None
            and existing.slot_start == slot[0]
            and existing.slot_end == slot[1]
        )
        if (
            existing_same_slot
            and existing is not None
            and abs(target_limit_w - existing.target_limit_w) <= 1.0
        ):
            # Repeated verification of an unchanged target must not turn the
            # bounded stale-feedback window into an indefinitely renewed one.
            return

        previous_reference_w = max(float(previous_live_limit_w), 0.0)
        if existing_same_slot and existing is not None:
            previous_reference_w = max(previous_reference_w, existing.previous_limit_w)

        if target_limit_w >= previous_reference_w - 1.0:
            if existing_same_slot:
                self._clear_primary_grid_charge_transition()
            return

        self._primary_grid_charge_transition = PrimaryGridChargeTransition(
            previous_limit_w=previous_reference_w,
            target_limit_w=target_limit_w,
            slot_start=slot[0],
            slot_end=slot[1],
            expires_at_monotonic=(
                monotonic() + PRIMARY_GRID_CHARGE_TRANSITION_MAX_SECONDS
            ),
        )
        self._primary_grid_charge_timed_out_slot = None
        self._schedule_primary_grid_charge_deadline(
            self._primary_grid_charge_transition
        )
