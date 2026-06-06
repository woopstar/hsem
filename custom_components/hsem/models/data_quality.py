"""Dataclass for structured diagnostics about planning input completeness."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class DataQuality:
    """Structured diagnostics about the completeness of planning inputs.

    Populated by :func:`~custom_components.hsem.planner.engine.run_planner`
    after aligning price and PV series.  All ``missing_*`` fields list
    wall-clock hours (0-23) for which data was absent.

    Attributes:
        tomorrow_price_missing_hours:
            Hours in tomorrow's slot grid (day_offset=1) that had no price data.
            Empty list means tomorrow price data is complete (or the
            planning horizon does not reach tomorrow).
        tomorrow_pv_missing_hours:
            Hours in tomorrow's slot grid (day_offset=1) that had no PV
            forecast data.  Empty list means complete (or not required).
        day2_price_missing_hours:
            Hours in the day-after-tomorrow slot grid (day_offset=2) that
            had no price data.  Empty when horizon is < 48 h or data is
            complete.
        day2_pv_missing_hours:
            Hours in the day-after-tomorrow slot grid (day_offset=2) that
            had no PV forecast data.
        today_price_missing_hours:
            Hours in today's slot grid that had no price data.
        today_pv_missing_hours:
            Hours in today's slot grid that had no PV forecast data.
        horizon_has_tomorrow:
            ``True`` when the planning horizon extends into tomorrow
            (``interval_length_hours`` > 24 or effectively spans midnight).
        horizon_days:
            Number of distinct calendar days covered by the horizon
            (1 = today only, 2 = today + tomorrow, 3 = today + 2 future days).
    """

    tomorrow_price_missing_hours: list[int] = field(default_factory=list)
    tomorrow_pv_missing_hours: list[int] = field(default_factory=list)
    day2_price_missing_hours: list[int] = field(default_factory=list)
    day2_pv_missing_hours: list[int] = field(default_factory=list)
    today_price_missing_hours: list[int] = field(default_factory=list)
    today_pv_missing_hours: list[int] = field(default_factory=list)
    horizon_has_tomorrow: bool = False
    horizon_days: int = 1

    @property
    def is_complete(self) -> bool:
        """Return ``True`` when no missing data was detected."""
        return not (
            self.tomorrow_price_missing_hours
            or self.tomorrow_pv_missing_hours
            or self.day2_price_missing_hours
            or self.day2_pv_missing_hours
            or self.today_price_missing_hours
            or self.today_pv_missing_hours
        )

    @property
    def tomorrow_price_complete(self) -> bool:
        """Return ``True`` when tomorrow price data is complete or not required."""
        return not self.tomorrow_price_missing_hours

    @property
    def tomorrow_pv_complete(self) -> bool:
        """Return ``True`` when tomorrow PV data is complete or not required."""
        return not self.tomorrow_pv_missing_hours

    def as_dict(self) -> dict[str, Any]:
        """Serialise the quality report to a plain dict for HA attributes.

        Returns:
            A JSON-safe dictionary representation of the data quality report.
        """
        return {
            "is_complete": self.is_complete,
            "horizon_has_tomorrow": self.horizon_has_tomorrow,
            "horizon_days": self.horizon_days,
            "tomorrow_price_missing_hours": sorted(self.tomorrow_price_missing_hours),
            "tomorrow_pv_missing_hours": sorted(self.tomorrow_pv_missing_hours),
            "day2_price_missing_hours": sorted(self.day2_price_missing_hours),
            "day2_pv_missing_hours": sorted(self.day2_pv_missing_hours),
            "today_price_missing_hours": sorted(self.today_price_missing_hours),
            "today_pv_missing_hours": sorted(self.today_pv_missing_hours),
        }
