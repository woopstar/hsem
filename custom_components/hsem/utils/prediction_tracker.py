"""Prediction-vs-actual tracking for planner accuracy metrics.

Tracks SoC prediction MAE, solar MAPE, load MAE, and action mix
over rolling windows.  No Home Assistant dependencies —
pure Python, testable with plain ``pytest``.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    history_file: str = ""

    records: list[PredictionRecord] = field(default_factory=list)

    # Computed metrics (updated when records are added)
    soc_mae_7d: float | None = None
    soc_mae_30d: float | None = None
    solar_mape: float | None = None
    load_mae_kwh: float | None = None
    action_mix: dict[str, float] = field(default_factory=dict)

    _warmup_slots: int = 4  # Skip first 4 slots (1 hour) after restart
    _slots_seen: int = field(default=0, repr=False)
    _seen_starts: set[datetime] = field(default_factory=set, repr=False)
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
    ) -> bool:
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
        if slot_start in self._seen_starts:
            return False
        self._seen_starts.add(slot_start)

        self._slots_seen += 1

        if self._slots_seen <= self._warmup_slots:
            return False

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
        return True

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

    def to_persistence_dict(self) -> dict[str, Any]:
        """Return bounded history for JSON persistence."""
        self._prune()
        return {
            "version": 1,
            "updated_at": datetime.now(UTC).isoformat(),
            "records": [record.as_dict() for record in self.records],
        }

    def load_from_dict(self, data: Mapping[str, Any]) -> None:
        """Replace current history with validated persisted records."""
        restored: dict[datetime, PredictionRecord] = {}
        raw_records = data.get("records", [])
        if isinstance(raw_records, list):
            for raw in raw_records[-self.max_records :]:
                if isinstance(raw, Mapping):
                    record = PredictionRecord.from_dict(raw)
                    if record is not None:
                        restored[record.slot_start.astimezone(UTC)] = record
        self.records = sorted(
            restored.values(), key=lambda record: record.slot_start.astimezone(UTC)
        )[-self.max_records :]
        self._recorded_starts = {record.slot_start for record in self.records}
        self._seen_starts = set(self._recorded_starts)
        self._slots_seen = self._warmup_slots if self.records else 0
        self.compute_metrics()

    async def load_history(self) -> None:
        """Load prediction history from disk when available."""
        if not self.history_file:
            return
        path = Path(self.history_file)
        if not path.exists():
            return
        data = await asyncio.to_thread(self._read_history_file, path)
        if isinstance(data, Mapping):
            self.load_from_dict(data)

    async def save_history(self) -> bool:
        """Persist prediction history atomically."""
        if not self.history_file:
            return False
        path = Path(self.history_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        return await asyncio.to_thread(
            self._write_history_file, path, self.to_persistence_dict()
        )

    @staticmethod
    def _read_history_file(path: Path) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError, OSError:
            return None

    @staticmethod
    def _write_history_file(path: Path, data: Mapping[str, Any]) -> bool:
        try:
            fd, temporary_path = tempfile.mkstemp(
                suffix=".json",
                prefix=".hsem_prediction_history_",
                dir=str(path.parent),
                text=True,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(data, handle, separators=(",", ":"), ensure_ascii=False)
                os.replace(temporary_path, path)
            except Exception:
                with suppress(OSError):
                    os.unlink(temporary_path)
                raise
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prune(self) -> None:
        """Remove the oldest records when the buffer exceeds the max size."""
        while len(self.records) > self.max_records:
            removed = self.records.pop(0)
            self._recorded_starts.discard(removed.slot_start)
            self._seen_starts.discard(removed.slot_start)
