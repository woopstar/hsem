"""Prediction-vs-actual tracking for planner accuracy metrics.

Tracks SoC prediction MAE, solar MAPE, load MAE, and action mix
over rolling windows.  No Home Assistant dependencies —
pure Python, testable with plain ``pytest``.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from custom_components.hsem.models.prediction_record import PredictionRecord

# 7 days × 4 slots/h × 24 h = 672
_SEVEN_DAY_SLOTS = 672
# 30 days × 4 slots/h × 24 h = 2880
_THIRTY_DAY_SLOTS = 2880


def _action_label(recommendation: str | None) -> str:
    """Map a recommendation string to a human-readable action label.

    Args:
        recommendation: The ``PlannedSlot.recommendation`` value, or ``None``.

    Returns:
        ``"charge"`` for any charging recommendation, ``"discharge"`` for
        any discharging recommendation, ``"idle"`` otherwise.
    """
    if recommendation is None:
        return "idle"
    if recommendation in {"batteries_charge_grid", "batteries_charge_solar"}:
        return "charge"
    if recommendation in {"batteries_discharge_mode", "force_batteries_discharge"}:
        return "discharge"
    return "idle"


@dataclass
class PredictionTracker:
    """Tracks prediction accuracy: SoC MAE, solar MAPE, action mix.

    The tracker maintains a rolling buffer of :class:`PredictionRecord`
    entries and recomputes aggregate metrics on every addition.

    Attributes:
        max_records: Maximum number of records to retain (default 672 = 7 days
            at 15-minute slots).
        records: Rolling buffer of prediction-vs-actual records, oldest first.
        soc_mae_7d: Mean absolute error of SoC prediction (%) over the last
            7 days (or less when fewer records exist).
        soc_mae_30d: Mean absolute error of SoC prediction (%) over the last
            30 days (limited to available data).
        solar_mape: Mean absolute percentage error of PV forecast (%).
            ``None`` when no actual PV data exists.
        load_mae_kwh: Mean absolute error of load prediction (kWh).
        action_mix: Fraction of records per action label (e.g.
            ``{"charge": 0.15, "discharge": 0.10, "idle": 0.75}``).
    """

    max_records: int = 672  # 7 days at 15-min = 672 slots

    records: list[PredictionRecord] = field(default_factory=list)

    # Computed metrics (updated when records are added)
    soc_mae_7d: float | None = None
    soc_mae_30d: float | None = None
    solar_mape: float | None = None
    load_mae_kwh: float | None = None
    action_mix: dict[str, float] = field(default_factory=dict)

    _warmup_slots: int = 4  # Skip first 4 slots (1 hour) after restart
    _slots_seen: int = field(default=0, repr=False)
    _recorded_starts: set[datetime] = field(default_factory=set, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_record(
        self,
        predicted_soc: float,
        actual_soc: float,
        predicted_pv: float,
        actual_pv: float,
        predicted_load: float,
        actual_load: float,
        action: str,
        slot_start: datetime,
    ) -> None:
        """Add a prediction-vs-actual record for a completed slot.

        The warm-up gate silently drops the first ``_warmup_slots`` slots
        to avoid cold-start artifacts after a coordinator restart.
        Already-recorded slot starts are silently ignored.

        Args:
            predicted_soc: Planner-predicted battery SoC (%) at end of slot.
            actual_soc: Actual battery SoC (%) at end of slot.
            predicted_pv: Forecast PV production for this slot (kWh).
            actual_pv: Actual PV production during this slot (kWh).
            predicted_load: Predicted house load for this slot (kWh).
            actual_load: Actual house load during this slot (kWh).
            action: Human-readable action label
                (``"charge"``, ``"discharge"``, or ``"idle"``).
            slot_start: Timezone-aware start of the slot (used for
                deduplication).
        """
        if slot_start in self._recorded_starts:
            return

        self._slots_seen += 1

        if self._slots_seen <= self._warmup_slots:
            return

        self._recorded_starts.add(slot_start)

        record = PredictionRecord(
            slot_start=slot_start,
            predicted_soc_pct=predicted_soc,
            actual_soc_pct=actual_soc,
            predicted_pv_kwh=predicted_pv,
            actual_pv_kwh=actual_pv,
            predicted_load_kwh=predicted_load,
            actual_load_kwh=actual_load,
            action=action,
        )
        self.records.append(record)
        self._prune()
        self.compute_metrics()

    def compute_metrics(self) -> None:
        """Recompute MAE / MAPE / action mix from the rolling buffer.

        Callers do not normally need to invoke this themselves —
        :meth:`add_record` calls it automatically after every addition.
        """
        if not self.records:
            self.soc_mae_7d = None
            self.soc_mae_30d = None
            self.solar_mape = None
            self.load_mae_kwh = None
            self.action_mix = {}
            return

        # Select recent records for 7-day and 30-day windows.
        seven_day_cutoff = max(0, len(self.records) - _SEVEN_DAY_SLOTS)
        recent_7d = self.records[seven_day_cutoff:]

        thirty_day_cutoff = max(0, len(self.records) - _THIRTY_DAY_SLOTS)
        recent_30d = self.records[thirty_day_cutoff:]

        # SoC MAE — 7 day window
        soc_errors_7d = [abs(r.predicted_soc_pct - r.actual_soc_pct) for r in recent_7d]
        self.soc_mae_7d = statistics.mean(soc_errors_7d) if soc_errors_7d else None

        # SoC MAE — 30 day window (limited to available data)
        soc_errors_30d = [
            abs(r.predicted_soc_pct - r.actual_soc_pct) for r in recent_30d
        ]
        self.soc_mae_30d = statistics.mean(soc_errors_30d) if soc_errors_30d else None

        # Solar MAPE (7-day window, excludes slots with zero actual PV)
        pv_records = [r for r in recent_7d if abs(r.actual_pv_kwh) > 1e-9]
        if pv_records:
            pv_ape = [
                abs(r.predicted_pv_kwh - r.actual_pv_kwh) / abs(r.actual_pv_kwh)
                for r in pv_records
            ]
            self.solar_mape = statistics.mean(pv_ape) * 100.0
        else:
            self.solar_mape = None

        # Load MAE (7-day window)
        load_errors = [abs(r.predicted_load_kwh - r.actual_load_kwh) for r in recent_7d]
        self.load_mae_kwh = statistics.mean(load_errors) if load_errors else None

        # Action mix (7-day window)
        action_counts: dict[str, int] = defaultdict(int)
        for r in recent_7d:
            action_counts[r.action] += 1
        total = len(recent_7d)
        self.action_mix = {
            action: count / total for action, count in action_counts.items()
        }

    def reset_warmup(self) -> None:
        """Reset the warm-up counter so the next *warmup_slots* are skipped.

        Useful in tests or after a coordinator restart to guarantee the
        warm-up gate is active.
        """
        self._slots_seen = 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Remove the oldest records when the buffer exceeds the max size."""
        while len(self.records) > self.max_records:
            removed = self.records.pop(0)
            self._recorded_starts.discard(removed.slot_start)
