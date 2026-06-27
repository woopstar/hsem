"""Weekday/weekend split house load curves using EWMA smoothing.

Provides a :class:`WeekdayProfile` that maintains separate 24-hour
consumption profiles for weekdays (Mon–Fri) and weekends (Sat–Sun).
Updated on each sensor cycle and read by the consumption populator
when ML history is not yet available.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class WeekdayProfile:
    """24-slot EWMA profiles split by weekday (Mon-Fri) vs weekend (Sat-Sun).

    Maintains two 24-element lists — one for weekdays, one for
    weekends — each updated via exponential weighted moving average
    (EWMA) from live consumption data.
    """

    slots_per_day: int = 24  # 24 hourly slots

    weekday: list[float] = field(default_factory=lambda: [0.0] * 24)
    weekend: list[float] = field(default_factory=lambda: [0.0] * 24)

    # EWMA smoothing factor (higher = more weight on recent samples).
    # An alpha of 0.15 gives an effective window of roughly 7 days.
    alpha: float = 0.15

    def __post_init__(self) -> None:
        """Ensure weekday and weekend lists are properly initialised."""
        if not self.weekday:
            self.weekday = [0.0] * self.slots_per_day
        if not self.weekend:
            self.weekend = [0.0] * self.slots_per_day

    def update(self, dow: int, slot: int, value_kwh: float) -> None:
        """Update the appropriate profile with a new consumption sample.

        Args:
            dow: Day of week (0=Monday … 6=Sunday).
            slot: Hour slot (0–23).
            value_kwh: Consumption value in kWh to feed into the EWMA.
        """
        profile = self.weekend if dow >= 5 else self.weekday
        if 0 <= slot < self.slots_per_day:
            profile[slot] = profile[slot] * (1 - self.alpha) + value_kwh * self.alpha

    def get(self, dow: int, slot: int) -> float:
        """Get the predicted consumption for a given day-of-week and hour slot.

        Args:
            dow: Day of week (0=Monday … 6=Sunday).
            slot: Hour slot (0–23).

        Returns:
            EWMA-smoothed consumption in kWh, or 0.0 if the slot is
            out of range.
        """
        profile = self.weekend if dow >= 5 else self.weekday
        if 0 <= slot < self.slots_per_day:
            return profile[slot]
        return 0.0


# Module-level singleton shared across the sensor (writer) and the
# consumption populator (reader).
weekday_profile = WeekdayProfile()
