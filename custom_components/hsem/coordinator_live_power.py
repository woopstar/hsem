"""Rolling live house/PV power sampling and bounded corrective replanning.

Extracted from ``coordinator.py`` to satisfy the repository's 30 KB /
1000-line file limit.

Adapted from Ambilights/hsem-ambilights#29 ("stabilize current-slot live
power") and #31's follow-up budget-v2 refinement, with the fork's PowMr/
secondary-storage normalization stripped (this repo has no secondary
storage subsystem). Unlike the fork, which reuses an existing 10-second
force-discharge monitor tick, this repo registers a dedicated lightweight
timer (:meth:`CoordinatorLivePowerMixin.async_monitor_live_power`,
``LIVE_POWER_MONITOR_INTERVAL_SECONDS`` cadence) purely for this feature —
:class:`~custom_components.hsem.utils.live_power.LivePowerWindow` requires
samples fresher than ``LIVE_POWER_MAX_SAMPLE_AGE_SECONDS``, which the
existing per-cycle timer (minutes, not seconds) cannot provide.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.coordinator_state import CoordinatorSharedState
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.datetime_utils import slot_key, utc_key
from custom_components.hsem.utils.ha_helpers import ha_get_entity_state_and_convert
from custom_components.hsem.utils.live_power import LivePowerEstimate, LivePowerWindow
from custom_components.hsem.utils.logger import async_log

#: Dedicated live-power sampling cadence. Independent of the main
#: coordinator interval so the rolling window sees fresh, closely-spaced
#: samples regardless of the current full-cycle polling rate.
LIVE_POWER_MONITOR_INTERVAL_SECONDS = 10
LIVE_POWER_WINDOW_SECONDS = 60
LIVE_POWER_MINIMUM_SAMPLES = 3
LIVE_POWER_MAX_SAMPLE_AGE_SECONDS = 20
LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS = 30
LIVE_POWER_REPLAN_MIN_REMAINING_SECONDS = 60
LIVE_POWER_REPLAN_MIN_DELTA_KWH = 0.05
LIVE_POWER_REPLAN_RELATIVE_DELTA = 0.10
#: One initial correction plus one debounced, provably-opposite-direction
#: reversal (e.g. a cloud dip followed by a genuine PV rebound) — never
#: unbounded solve churn within a single slot.
LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT = 2

type LivePowerSourceSignature = tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    bool,
]


class CoordinatorLivePowerMixin(CoordinatorSharedState):
    """Rolling live house/PV power sampling and bounded corrective replanning."""

    # ------------------------------------------------------------------
    # Window lifecycle
    # ------------------------------------------------------------------

    def _live_power_window_instance(self) -> LivePowerWindow:
        """Return the coordinator window, lazily creating it for test fixtures."""
        window = getattr(self, "_live_power_window", None)
        if isinstance(window, LivePowerWindow):
            return window
        window = LivePowerWindow(
            window_seconds=LIVE_POWER_WINDOW_SECONDS,
            minimum_samples=LIVE_POWER_MINIMUM_SAMPLES,
            maximum_sample_age_seconds=LIVE_POWER_MAX_SAMPLE_AGE_SECONDS,
        )
        self._live_power_window = window
        return window

    @staticmethod
    def _live_power_entity_id(value: object) -> str | None:
        """Return a configured entity ID without accepting mock/sentinel values."""
        return value if isinstance(value, str) and value else None

    def _live_power_source_key(self, cfg: SensorConfig) -> LivePowerSourceSignature:
        """Return every source/config value that changes sample interpretation."""
        return (
            self._live_power_entity_id(cfg.house_consumption_power),
            self._live_power_entity_id(cfg.solar_production_power),
            self._live_power_entity_id(cfg.ev.status_entity),
            self._live_power_entity_id(cfg.ev.power_entity),
            self._live_power_entity_id(cfg.ev_second.status_entity),
            self._live_power_entity_id(cfg.ev_second.power_entity),
            cfg.house_power_includes_ev_charger_power is True,
        )

    def _prepare_live_power_window(self, cfg: SensorConfig) -> LivePowerWindow:
        """Reset retained authority after a power source or topology change."""
        window = self._live_power_window_instance()
        signature = self._live_power_source_key(cfg)
        previous = getattr(self, "_live_power_source_signature", None)
        if previous is not None and previous != signature:
            window.clear()
            self._reset_live_power_replan_budget()
        self._live_power_source_signature = signature
        return window

    def reset_live_power_state(self) -> None:
        """Clear all retained live-power state (config reload / teardown)."""
        window = getattr(self, "_live_power_window", None)
        if isinstance(window, LivePowerWindow):
            window.clear()
        self._reset_live_power_replan_budget()
        self._live_power_source_signature = None
        self._last_plan_live_power_estimate = None

    # ------------------------------------------------------------------
    # Sample interpretation
    # ------------------------------------------------------------------

    @staticmethod
    def _canonical_live_power_number(
        value: object,
        *,
        allow_negative: bool = False,
    ) -> float | None:
        """Return one finite physical power reading or None."""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number) or (not allow_negative and number < 0.0):
            return None
        return number

    @classmethod
    def _snapshot_live_power_number(
        cls,
        entity_id: object,
        value: object,
        missing_entities: list[str] | tuple[str, ...],
        *,
        missing_label: str,
        allow_negative: bool = False,
    ) -> float | None:
        """Resolve a snapshot number without promoting its numeric fallback."""
        if cls._live_power_entity_id(entity_id) is None:
            return None
        if any(missing_label in item for item in missing_entities):
            return None
        return cls._canonical_live_power_number(value, allow_negative=allow_negative)

    @classmethod
    def _live_power_ev_ambiguous(
        cls,
        cfg: SensorConfig,
        live: LiveState | None,
    ) -> bool:
        """Fail closed across an inclusive-EV status flicker.

        When the house-power meter already includes EV charger load
        (``house_power_includes_ev_charger_power``), a live/planned EV
        charging signal makes the raw house reading unusable as planner-base
        demand — it cannot be disentangled from the EV's own draw.
        """
        if cfg.house_power_includes_ev_charger_power is not True or live is None:
            return False
        if live.any_ev_charging:
            return True
        for ev_live in (live.ev, live.ev_second):
            power_w = cls._canonical_live_power_number(
                ev_live.power_w, allow_negative=True
            )
            if power_w is not None and power_w > 1e-9:
                return True
        return False

    def _seed_live_power_window(
        self,
        now: datetime,
        cfg: SensorConfig,
        live: LiveState,
    ) -> LivePowerEstimate:
        """Seed the rolling window from the full-cycle immutable snapshot."""
        window = self._prepare_live_power_window(cfg)
        raw_missing = getattr(live, "missing_entities_list", ())
        missing_entities = raw_missing if isinstance(raw_missing, (list, tuple)) else ()
        house_w = self._snapshot_live_power_number(
            cfg.house_consumption_power,
            live.house_consumption_power_w,
            missing_entities,
            missing_label="house_consumption_power",
        )
        if self._live_power_ev_ambiguous(cfg, live):
            house_w = None
        solar_w = self._snapshot_live_power_number(
            cfg.solar_production_power,
            live.solar_production_power_w,
            missing_entities,
            missing_label="solar_production_power",
        )
        window.add_sample(
            now,
            house_power_w=house_w,
            solar_power_w=solar_w,
            house_available=house_w is not None,
            solar_available=solar_w is not None,
        )
        return window.estimate(now)

    # ------------------------------------------------------------------
    # Fast-timer sampling (independent HA reads between full cycles)
    # ------------------------------------------------------------------

    def _read_live_power_number(
        self,
        entity_id: object,
        *,
        allow_negative: bool = False,
    ) -> float | None:
        """Read one timer sample without coupling another channel's validity."""
        resolved = self._live_power_entity_id(entity_id)
        if resolved is None:
            return None
        try:
            raw_value = ha_get_entity_state_and_convert(self, resolved, "float", 3)
        except HomeAssistantError, ValueError, TypeError, AttributeError:
            return None
        return self._canonical_live_power_number(
            raw_value, allow_negative=allow_negative
        )

    def _read_live_power_boolean(self, entity_id: object) -> bool | None:
        """Read one timer boolean without treating an unavailable state as false."""
        resolved = self._live_power_entity_id(entity_id)
        if resolved is None:
            return None
        try:
            raw_value = ha_get_entity_state_and_convert(self, resolved, "boolean", 3)
        except HomeAssistantError, ValueError, TypeError, AttributeError:
            return None
        return raw_value if isinstance(raw_value, bool) else None

    def _read_live_power_positive_raw(self, entity_id: object) -> bool:
        """Return whether a timer EV-power endpoint is positive in its raw unit."""
        resolved = self._live_power_entity_id(entity_id)
        if resolved is None:
            return False
        try:
            raw_value = ha_get_entity_state_and_convert(self, resolved, "float", 6)
        except HomeAssistantError, ValueError, TypeError, AttributeError:
            return False
        number = self._canonical_live_power_number(raw_value, allow_negative=True)
        return number is not None and number > 1e-9

    def _live_power_tick_ev_ambiguous(
        self,
        cfg: SensorConfig,
        live: LiveState | None,
    ) -> bool:
        """Combine snapshot and same-tick EV signals for an inclusive meter."""
        if cfg.house_power_includes_ev_charger_power is not True:
            return False
        if self._live_power_ev_ambiguous(cfg, live):
            return True
        for charger in (cfg.ev, cfg.ev_second):
            if self._read_live_power_boolean(charger.status_entity) is True:
                return True
            if self._read_live_power_positive_raw(charger.power_entity):
                return True
        return False

    def _sample_live_power_window(
        self, now: datetime
    ) -> tuple[LivePowerEstimate, float | None, float | None, bool]:
        """Sample raw meters via fresh HA reads and update the rolling window."""
        cfg = self._cfg
        live = self._live
        window = self._prepare_live_power_window(cfg)
        raw_house_w = self._read_live_power_number(cfg.house_consumption_power)
        solar_w = self._read_live_power_number(cfg.solar_production_power)
        ev_ambiguous = self._live_power_tick_ev_ambiguous(cfg, live)
        house_w = None if ev_ambiguous else raw_house_w
        window.add_sample(
            now,
            house_power_w=house_w,
            solar_power_w=solar_w,
            house_available=house_w is not None,
            solar_available=solar_w is not None,
        )
        return window.estimate(now), raw_house_w, solar_w, ev_ambiguous

    # ------------------------------------------------------------------
    # Materiality and slot bookkeeping
    # ------------------------------------------------------------------

    @staticmethod
    def _live_power_channel_changed_materially(
        current_w: float | None,
        accepted_w: float | None,
        *,
        slot_hours: float,
    ) -> bool:
        """Compare one channel using full-slot energy and relative thresholds."""
        if current_w is None or accepted_w is None:
            return current_w is not None or accepted_w is not None
        delta_kwh = abs(current_w - accepted_w) * slot_hours / 1000.0
        accepted_kwh = abs(accepted_w) * slot_hours / 1000.0
        threshold_kwh = max(
            LIVE_POWER_REPLAN_MIN_DELTA_KWH,
            accepted_kwh * LIVE_POWER_REPLAN_RELATIVE_DELTA,
        )
        return delta_kwh + 1e-9 >= threshold_kwh

    def _live_power_estimate_changed_materially(
        self,
        estimate: LivePowerEstimate,
        *,
        house_ambiguous: bool | None = None,
    ) -> bool:
        """Return whether a fresh estimate differs from accepted planner input."""
        accepted = getattr(self, "_last_plan_live_power_estimate", None)
        try:
            slot_hours = float(self._cfg.recommendation_interval_minutes) / 60.0
        except TypeError, ValueError:
            slot_hours = 0.25
        if not math.isfinite(slot_hours) or slot_hours <= 0.0:
            slot_hours = 0.25

        solar_changed = self._live_power_channel_changed_materially(
            estimate.solar_power_w,
            accepted.solar_power_w if accepted is not None else None,
            slot_hours=slot_hours,
        )
        if house_ambiguous is None:
            live = getattr(self, "_live", None)
            house_ambiguous = self._live_power_ev_ambiguous(self._cfg, live)
        house_changed = False
        if not house_ambiguous:
            house_changed = self._live_power_channel_changed_materially(
                estimate.house_power_w,
                accepted.house_power_w if accepted is not None else None,
                slot_hours=slot_hours,
            )
        return house_changed or solar_changed

    def _live_power_slot_context(self, now: datetime) -> tuple[datetime, float]:
        """Return current canonical slot start and physical seconds remaining."""
        try:
            interval_minutes = int(self._cfg.recommendation_interval_minutes)
        except TypeError, ValueError:
            interval_minutes = 15
        if interval_minutes <= 0:
            interval_minutes = 15
        current_slot = slot_key(now, interval_minutes)
        slot_end = utc_key(current_slot) + timedelta(minutes=interval_minutes)
        remaining_seconds = (slot_end - utc_key(now)).total_seconds()
        return current_slot, remaining_seconds

    def _clear_live_power_replan_state(self) -> None:
        """Clear the in-progress mismatch and any not-yet-accepted request."""
        self._live_power_mismatch_since = None
        self._live_power_mismatch_slot_start = None
        self._live_power_replan_pending_slot = None

    def _reset_live_power_replan_budget(self) -> None:
        """Clear completed same-slot corrections and reversal authority."""
        self._clear_live_power_replan_state()
        self._live_power_replanned_slot_start = None
        self._live_power_replan_count = 0
        self._live_power_first_replan_direction = None

    # ------------------------------------------------------------------
    # Replan budget (issue #797 — one correction + one proven reversal)
    # ------------------------------------------------------------------

    @staticmethod
    def _live_power_site_balance_direction(
        estimate: LivePowerEstimate,
        accepted: LivePowerEstimate | None,
        *,
        house_ambiguous: bool,
    ) -> int | None:
        """Return the signed net-demand change, or None when it is unprovable.

        Positive means net demand increased (more house draw / less solar);
        negative means it decreased. Used only to prove a reversal is the
        opposite direction of the first correction in the slot, never to
        decide whether to replan at all.
        """
        if accepted is None:
            return None
        delta_w = 0.0
        has_comparable_channel = False
        if (
            not house_ambiguous
            and estimate.house_power_w is not None
            and accepted.house_power_w is not None
        ):
            house_delta_w = estimate.house_power_w - accepted.house_power_w
            if math.isfinite(house_delta_w):
                delta_w += house_delta_w
                has_comparable_channel = True
        if estimate.solar_power_w is not None and accepted.solar_power_w is not None:
            solar_delta_w = accepted.solar_power_w - estimate.solar_power_w
            if math.isfinite(solar_delta_w):
                delta_w += solar_delta_w
                has_comparable_channel = True
        if not has_comparable_channel or abs(delta_w) <= 1e-9:
            return None
        return 1 if delta_w > 0.0 else -1

    def _live_power_replan_budget_allows(
        self,
        current_slot: datetime,
        estimate: LivePowerEstimate,
        *,
        house_ambiguous: bool | None = None,
    ) -> bool:
        """Allow a first correction, then only one opposite-direction reversal."""
        replanned_slot = getattr(self, "_live_power_replanned_slot_start", None)
        if replanned_slot is None or utc_key(replanned_slot) != utc_key(current_slot):
            return True

        completed: int = getattr(self, "_live_power_replan_count", 1)
        if completed >= LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT:
            return False
        if completed <= 0:
            return True

        first_direction: int | None = getattr(
            self, "_live_power_first_replan_direction", None
        )
        if first_direction not in (-1, 1):
            return False
        if house_ambiguous is None:
            live = getattr(self, "_live", None)
            house_ambiguous = self._live_power_ev_ambiguous(self._cfg, live)
        current_direction = self._live_power_site_balance_direction(
            estimate,
            getattr(self, "_last_plan_live_power_estimate", None),
            house_ambiguous=house_ambiguous,
        )
        return current_direction == -first_direction

    # ------------------------------------------------------------------
    # Mismatch tracking and replan requests
    # ------------------------------------------------------------------

    def _track_live_power_mismatch(
        self,
        now: datetime,
        estimate: LivePowerEstimate,
        *,
        house_ambiguous: bool | None = None,
    ) -> bool:
        """Advance the sustained-mismatch debounce and return pending status."""
        current_slot, remaining_seconds = self._live_power_slot_context(now)
        last_plan_at = getattr(self, "_last_plan_slot_start", None)
        if (
            last_plan_at is None
            or slot_key(last_plan_at, self._cfg.recommendation_interval_minutes)
            != current_slot
            or remaining_seconds < LIVE_POWER_REPLAN_MIN_REMAINING_SECONDS
        ):
            self._clear_live_power_replan_state()
            return False

        if not self._live_power_estimate_changed_materially(
            estimate, house_ambiguous=house_ambiguous
        ):
            self._clear_live_power_replan_state()
            return False
        if not self._live_power_replan_budget_allows(
            current_slot, estimate, house_ambiguous=house_ambiguous
        ):
            self._clear_live_power_replan_state()
            return False

        pending_slot = getattr(self, "_live_power_replan_pending_slot", None)
        if pending_slot is not None and utc_key(pending_slot) == utc_key(current_slot):
            return True

        mismatch_slot = getattr(self, "_live_power_mismatch_slot_start", None)
        mismatch_since = getattr(self, "_live_power_mismatch_since", None)
        if (
            mismatch_slot is None
            or utc_key(mismatch_slot) != utc_key(current_slot)
            or mismatch_since is None
        ):
            self._live_power_mismatch_slot_start = current_slot
            self._live_power_mismatch_since = now
            return False

        elapsed_seconds = (utc_key(now) - utc_key(mismatch_since)).total_seconds()
        if elapsed_seconds < LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS:
            return False

        self._live_power_replan_pending_slot = current_slot
        async_log(
            "debug",
            "[replan] Sustained live power changed materially for %ds: "
            "slot=%s house=%sW pv=%sW; requesting bounded same-slot replan.",
            LIVE_POWER_MISMATCH_DEBOUNCE_SECONDS,
            current_slot.isoformat(),
            estimate.house_power_w,
            estimate.solar_power_w,
        )
        return True

    def _actionable_live_power_replan_slot(
        self,
        now: datetime,
        estimate: LivePowerEstimate,
    ) -> datetime | None:
        """Revalidate a durable request against current slot and fresh evidence."""
        pending_slot = getattr(self, "_live_power_replan_pending_slot", None)
        if pending_slot is None:
            return None
        current_slot, remaining_seconds = self._live_power_slot_context(now)
        last_plan_at = getattr(self, "_last_plan_slot_start", None)
        actionable = (
            utc_key(pending_slot) == utc_key(current_slot)
            and remaining_seconds >= LIVE_POWER_REPLAN_MIN_REMAINING_SECONDS
            and last_plan_at is not None
            and slot_key(last_plan_at, self._cfg.recommendation_interval_minutes)
            == current_slot
            and self._live_power_estimate_changed_materially(estimate)
            and self._live_power_replan_budget_allows(current_slot, estimate)
        )
        if actionable:
            return current_slot
        self._clear_live_power_replan_state()
        return None

    def _accept_live_power_plan_estimate(
        self,
        estimate: LivePowerEstimate,
        *,
        plan_now: datetime,
        requested_slot: datetime | None,
    ) -> None:
        """Advance aggregate baselines only after an accepted publication."""
        current_slot, _remaining_seconds = self._live_power_slot_context(plan_now)
        accepted_before = getattr(self, "_last_plan_live_power_estimate", None)
        old_pending = getattr(self, "_live_power_replan_pending_slot", None)
        old_mismatch_slot = getattr(self, "_live_power_mismatch_slot_start", None)
        old_mismatch_since = getattr(self, "_live_power_mismatch_since", None)
        consumed_request = requested_slot is not None and utc_key(
            requested_slot
        ) == utc_key(current_slot)

        self._last_plan_live_power_estimate = estimate
        self._clear_live_power_replan_state()
        if consumed_request:
            replanned_slot = getattr(self, "_live_power_replanned_slot_start", None)
            same_budget_slot = replanned_slot is not None and utc_key(
                replanned_slot
            ) == utc_key(current_slot)
            completed: int = (
                getattr(self, "_live_power_replan_count", 1) if same_budget_slot else 0
            )
            if completed <= 0:
                live = getattr(self, "_live", None)
                house_ambiguous = self._live_power_ev_ambiguous(self._cfg, live)
                self._live_power_first_replan_direction = (
                    self._live_power_site_balance_direction(
                        estimate,
                        accepted_before,
                        house_ambiguous=house_ambiguous,
                    )
                )
            self._live_power_replanned_slot_start = current_slot
            self._live_power_replan_count = min(
                completed + 1, LIVE_POWER_REPLAN_MAX_CORRECTIONS_PER_SLOT
            )
            return

        # A timer sample may arrive while the executor is solving. Preserve a
        # still-material pending/mismatch against the exact frozen estimate
        # that built this published plan; never consume newer evidence.
        from custom_components.hsem.utils.datetime_utils import now as hsem_now

        latest_now = hsem_now()
        if (
            slot_key(latest_now, self._cfg.recommendation_interval_minutes)
            != current_slot
        ):
            return
        latest = self._live_power_window_instance().estimate(latest_now)
        if not self._live_power_estimate_changed_materially(latest):
            return
        if old_pending is not None and utc_key(old_pending) == utc_key(current_slot):
            self._live_power_replan_pending_slot = current_slot
            return
        self._live_power_mismatch_slot_start = current_slot
        if (
            old_mismatch_slot is not None
            and utc_key(old_mismatch_slot) == utc_key(current_slot)
            and old_mismatch_since is not None
        ):
            self._live_power_mismatch_since = old_mismatch_since
        else:
            self._live_power_mismatch_since = latest_now

    # ------------------------------------------------------------------
    # Dedicated fast timer
    # ------------------------------------------------------------------

    async def async_monitor_live_power(self, now: datetime | None = None) -> None:
        """Ten-second tick: sample live power and request a bounded replan.

        Registered independently of the main coordinator interval timer
        (see :meth:`CoordinatorLifecycleMixin.async_setup`) because the
        rolling window needs samples fresher than
        ``LIVE_POWER_MAX_SAMPLE_AGE_SECONDS``.
        """
        from custom_components.hsem.utils.datetime_utils import now as hsem_now
        from custom_components.hsem.utils.degraded_mode import DegradedMode

        if getattr(self, "_tearing_down", False):
            return
        live = getattr(self, "_live", None)
        cfg = getattr(self, "_cfg", None)
        if live is None or cfg is None:
            return

        sample_now = now if now is not None else hsem_now()
        estimate, _house_power, _solar_power, ev_ambiguous = (
            self._sample_live_power_window(sample_now)
        )

        live_replan_eligible = (
            live.force_working_mode_state == "auto"
            and live.degraded_mode is not DegradedMode.Error
            and getattr(self, "_last_load_forecast_readiness_reason", None) is None
        )
        if not live_replan_eligible:
            self._clear_live_power_replan_state()
            return

        pending = self._track_live_power_mismatch(
            sample_now, estimate, house_ambiguous=ev_ambiguous
        )
        if pending and not self._update_lock.locked():
            await self._async_handle_update(None)
