"""Robust short-window aggregation for planner live-power inputs.

The planner projects current house and PV power across the active slot.  A
single boundary sample is therefore too influential: a short appliance spike
or cloud edge can invert the projected slot balance.  This module keeps a
small, in-memory median window without depending on Home Assistant runtime
types, so the sampling and availability rules remain deterministic and easy to
test.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class LivePowerEstimate:
    """Median live house/PV power and the evidence behind each channel."""

    house_power_w: float | None
    solar_power_w: float | None
    house_sample_count: int
    solar_sample_count: int

    @property
    def house_available(self) -> bool:
        """Return whether house power has enough fresh valid samples."""
        return self.house_power_w is not None

    @property
    def solar_available(self) -> bool:
        """Return whether PV power has enough fresh valid samples."""
        return self.solar_power_w is not None

    @property
    def complete(self) -> bool:
        """Return whether both channels have robust estimates."""
        return self.house_available and self.solar_available


class LivePowerWindow:
    """Maintain independent short rolling medians for house and PV power.

    Invalid or unavailable input clears only the affected channel.  This is
    intentionally fail-closed: a reading from before an availability gap must
    not silently become current planner authority after recovery.  Valid 0 W
    remains a normal sample, which is essential for PV after sunset or during
    sustained heavy cloud.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        minimum_samples: int,
        maximum_sample_age_seconds: float,
    ) -> None:
        """Initialise a bounded physical-time sample window."""
        if window_seconds <= 0.0:
            raise ValueError("window_seconds must be positive")
        if minimum_samples <= 0:
            raise ValueError("minimum_samples must be positive")
        if maximum_sample_age_seconds <= 0.0:
            raise ValueError("maximum_sample_age_seconds must be positive")
        self._window = timedelta(seconds=float(window_seconds))
        self._minimum_samples = int(minimum_samples)
        self._maximum_sample_age = timedelta(seconds=float(maximum_sample_age_seconds))
        self._house: deque[tuple[datetime, float]] = deque()
        self._solar: deque[tuple[datetime, float]] = deque()

    def clear(self) -> None:
        """Discard every retained channel sample."""
        self._house.clear()
        self._solar.clear()

    def add_sample(
        self,
        timestamp: datetime,
        *,
        house_power_w: float | None,
        solar_power_w: float | None,
        house_available: bool,
        solar_available: bool,
    ) -> None:
        """Add one timestamped endpoint, clearing unavailable channels."""
        instant = self._utc_instant(timestamp)
        self._add_channel(
            self._house,
            instant,
            house_power_w,
            available=house_available,
        )
        self._add_channel(
            self._solar,
            instant,
            solar_power_w,
            available=solar_available,
        )
        self._prune(instant)

    def estimate(self, now: datetime) -> LivePowerEstimate:
        """Return fresh channel medians when enough evidence is available."""
        instant = self._utc_instant(now)
        self._prune(instant)
        house = self._channel_estimate(self._house, instant)
        solar = self._channel_estimate(self._solar, instant)
        return LivePowerEstimate(
            house_power_w=house,
            solar_power_w=solar,
            house_sample_count=len(self._house),
            solar_sample_count=len(self._solar),
        )

    @staticmethod
    def _utc_instant(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("live-power timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _valid_power(value: float | None) -> bool:
        return value is not None and math.isfinite(value) and value >= 0.0

    def _add_channel(
        self,
        samples: deque[tuple[datetime, float]],
        timestamp: datetime,
        value: float | None,
        *,
        available: bool,
    ) -> None:
        if not available or not self._valid_power(value):
            samples.clear()
            return
        assert value is not None
        if samples and timestamp < samples[-1][0]:
            return
        if samples and timestamp == samples[-1][0]:
            samples[-1] = (timestamp, float(value))
            return
        samples.append((timestamp, float(value)))

    def _prune(self, now: datetime) -> None:
        cutoff = now - self._window
        for samples in (self._house, self._solar):
            while samples and samples[0][0] < cutoff:
                samples.popleft()

    def _channel_estimate(
        self,
        samples: deque[tuple[datetime, float]],
        now: datetime,
    ) -> float | None:
        if len(samples) < self._minimum_samples:
            return None
        age = now - samples[-1][0]
        if age < timedelta(0) or age > self._maximum_sample_age:
            return None
        return float(statistics.median(item[1] for item in samples))
