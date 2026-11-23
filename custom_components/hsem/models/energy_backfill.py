"""Daily energy records built from hourly long-term statistics (issue #1450).

The energy tracker only knows the days HSEM ran.  Home Assistant keeps hourly
statistics of the same meters from before that, so the days before the first
measured one can be taken from there.

A backfilled day follows the rules of a measured one: a quantity is only
added, and its hours only counted, in the hours that have statistics for it.
Hourly statistics are coarser than the live readings, which shows in the
battery figures:

- An hourly mean of the signed battery power is the net of the hour, so what
  was charged and discharged within one hour cancels out.  The battery charge
  and discharge of a backfilled day, and its cycles and wear cost, are
  therefore a lower bound.
- A backfilled day adds nothing to the measured round-trip loss: the net of
  an hour would bias it, and there is no SoC to correct with.
- The wear cost uses today's cycle cost, which follows from the configured
  purchase price and expected cycles.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, tzinfo

from custom_components.hsem.models.energy_tracker import BatterySite, EnergyDayEntry


@dataclass(frozen=True)
class HourlyEnergy:
    """Energy per UTC hour start, in kWh.  An hour without statistics is absent.

    Attributes:
        house_kwh: House consumption, EV charging included.
        grid_import_kwh: Grid import.
        grid_export_kwh: Grid export.
        pv_kwh: PV production.
        battery_net_kwh: Net energy into the battery, negative when the hour
            discharged more than it charged.
    """

    house_kwh: Mapping[datetime, float] = field(default_factory=dict)
    grid_import_kwh: Mapping[datetime, float] = field(default_factory=dict)
    grid_export_kwh: Mapping[datetime, float] = field(default_factory=dict)
    pv_kwh: Mapping[datetime, float] = field(default_factory=dict)
    battery_net_kwh: Mapping[datetime, float] = field(default_factory=dict)

    def hours(self) -> list[datetime]:
        """Return every hour that has at least one quantity, oldest first."""
        return sorted(
            {
                *self.house_kwh,
                *self.grid_import_kwh,
                *self.grid_export_kwh,
                *self.pv_kwh,
                *self.battery_net_kwh,
            }
        )


def build_backfill_days(
    hourly: HourlyEnergy, time_zone: tzinfo | None, site: BatterySite
) -> list[EnergyDayEntry]:
    """Return one daily record per local day the statistics cover.

    Args:
        hourly: The hourly energy read from the statistics.
        time_zone: The local time zone; an hour is booked on the local day
            it starts in.
        site: Today's battery capacities and cycle cost.  They give the
            backfilled days their full equivalent cycles and wear cost.

    Returns:
        The days, oldest first, each marked as backfilled.
    """
    days: dict[str, EnergyDayEntry] = {}
    for hour in hourly.hours():
        key = hour.astimezone(time_zone).date().isoformat()
        entry = days.get(key)
        if entry is None:
            entry = days[key] = EnergyDayEntry(key, backfilled=True)
            entry.usable_capacity_kwh = max(site.usable_capacity_kwh, 0.0)
            entry.rated_capacity_kwh = max(site.rated_capacity_kwh, 0.0)
        entry.observed_hours += 1.0

        grid_import = hourly.grid_import_kwh.get(hour)
        if grid_import is not None:
            entry.grid_import_kwh += max(grid_import, 0.0)
            entry.grid_import_hours += 1.0
        grid_export = hourly.grid_export_kwh.get(hour)
        if grid_export is not None:
            entry.grid_export_kwh += max(grid_export, 0.0)
            entry.grid_export_hours += 1.0
        pv = hourly.pv_kwh.get(hour)
        if pv is not None:
            entry.pv_kwh += max(pv, 0.0)
            entry.pv_hours += 1.0
        house = hourly.house_kwh.get(hour)
        if house is not None:
            house = max(house, 0.0)
            entry.house_consumption_kwh += house
            entry.house_hours += 1.0
            if grid_import is not None:
                entry.paired_consumption_kwh += house
                entry.paired_import_kwh += max(grid_import, 0.0)
        battery = hourly.battery_net_kwh.get(hour)
        if battery is not None:
            entry.battery_charged_kwh += max(battery, 0.0)
            entry.battery_discharged_kwh += max(-battery, 0.0)
            entry.battery_hours += 1.0
            if site.cycle_cost_per_kwh is not None:
                entry.wear_cost += abs(battery) * site.cycle_cost_per_kwh
                entry.priced_throughput_kwh += abs(battery)
    return [days[key] for key in sorted(days)]
