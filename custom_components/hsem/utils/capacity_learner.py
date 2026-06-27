"""Learns battery usable capacity from BMS kWh-remaining and SoC readings.

Samples delta_kwh / delta_soc_pct in the 15–85 % SoC mid-range to estimate
the true usable capacity independently of the nameplate rating.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CapacityLearner:
    """Learns battery usable capacity from BMS kWh-remaining and SoC readings.

    Accumulates capacity samples (delta_kwh / delta_soc_pct × 100) when both
    readings are in the mid-range (15–85 % SoC).  Once enough samples are
    collected the :attr:`learned_capacity_kwh` property returns the median
    estimate; otherwise it returns ``None``.
    """

    MIN_SOC: float = field(default=15.0, init=False)
    """Lower bound of the mid-range SoC window (%)."""

    MAX_SOC: float = field(default=85.0, init=False)
    """Upper bound of the mid-range SoC window (%)."""

    MIN_SAMPLES: int = field(default=20, init=False)
    """Minimum number of samples required before returning a result."""

    MAX_SAMPLES: int = field(default=200, init=False)
    """Maximum number of samples retained (FIFO)."""

    MIN_DELTA_SOC: float = field(default=0.5, init=False)
    """Minimum SoC change between two readings to compute a sample (%)."""

    MIN_DELTA_KWH: float = field(default=0.01, init=False)
    """Minimum kWh change between two readings to compute a sample (kWh)."""

    MIN_CAPACITY: float = field(default=1.0, init=False)
    """Minimum plausible capacity (kWh).  Samples below this are discarded."""

    MAX_CAPACITY: float = field(default=100.0, init=False)
    """Maximum plausible capacity (kWh).  Samples above this are discarded."""

    samples: list[float] = field(default_factory=list)
    """Computed capacity samples in kWh."""

    _last_kwh: float | None = field(default=None, init=False, repr=False)
    """Previous non-mid-range kWh-remaining reading."""

    _last_soc: float | None = field(default=None, init=False, repr=False)
    """Previous non-mid-range SoC reading."""

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def learned_capacity_kwh(self) -> float | None:
        """Return the median capacity estimate, or ``None`` if not enough samples."""
        if len(self.samples) < self.MIN_SAMPLES:
            return None
        sorted_samples = sorted(self.samples)
        mid = len(sorted_samples) // 2
        return sorted_samples[mid]

    @property
    def sample_count(self) -> int:
        """Return the number of accumulated samples."""
        return len(self.samples)

    # ------------------------------------------------------------------
    # Update
    # ------------------------------------------------------------------

    def update(self, bms_kwh_remaining: float | None, soc_pct: float | None) -> None:
        """Accumulate a capacity sample if both readings are in the mid-range.

        When the SoC is outside [MIN_SOC, MAX_SOC] the current readings are
        stored as a reference point.  When the SoC enters the mid-range again,
        the delta from the stored reference is used to compute a capacity
        estimate.

        Args:
            bms_kwh_remaining: BMS-reported remaining energy in kWh, or ``None``
                if unavailable.
            soc_pct: Battery state-of-charge as a percentage (0–100), or ``None``
                if unavailable.
        """
        if bms_kwh_remaining is None or soc_pct is None:
            return

        if not (self.MIN_SOC <= soc_pct <= self.MAX_SOC):
            # Outside mid-range — store as reference for the next transition.
            self._last_kwh = bms_kwh_remaining
            self._last_soc = soc_pct
            return

        # SoC is in mid-range — compute delta from last reference.
        if self._last_kwh is not None and self._last_soc is not None:
            delta_kwh = abs(bms_kwh_remaining - self._last_kwh)
            delta_soc = abs(soc_pct - self._last_soc)
            if delta_soc > self.MIN_DELTA_SOC and delta_kwh > self.MIN_DELTA_KWH:
                capacity = delta_kwh / (delta_soc / 100.0)
                if self.MIN_CAPACITY < capacity < self.MAX_CAPACITY:
                    self.samples.append(capacity)
                    if len(self.samples) > self.MAX_SAMPLES:
                        self.samples = self.samples[-self.MAX_SAMPLES :]

        # Update reference for next mid-range reading.
        self._last_kwh = bms_kwh_remaining
        self._last_soc = soc_pct
