"""Rebuild a day for ``hsem.what_if`` from hourly statistics (issue #1455).

The what-if history holds 90 measured days.  Home Assistant keeps hourly
long-term statistics for far longer, so a day without a measured record can
be rebuilt from them: the house load and the PV of each hour from the
statistics, both prices from the hourly prices HSEM kept.

A rebuilt day is a :class:`~custom_components.hsem.models.what_if_history.RecordedDay`
like a measured one, and it is replayed by the same code.  It is the day as
it would have been with an idle battery:

- the SoC stays at the end-of-discharge SoC, so every run of the day starts
  and ends with an empty battery;
- the grid flows are what the load and the PV leave: import where the load is
  larger, export where the PV is.  The load the replay derives from them is
  therefore the load of the statistics, whatever the real battery did.

It is coarser than a measured day.  One slot is one hour, so price and load
peaks inside an hour are averaged out.  It is rebuilt with today's battery
limits, since none were stored for it.

A day is rebuilt only when every hour of it has a real price and a house
load.  Where a PV source is configured, the day also needs PV statistics
without a gap between its first and its last PV hour: a renamed PV sensor or
a gap must not turn a sunny day into one of pure grid import.  The hours
before the first and after the last PV hour count as no production, since an
inverter that is unavailable at night leaves no statistics for them.  Without
a PV source the site has no PV and the production is zero.

Pure functions with no Home Assistant imports.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from custom_components.hsem.models.energy_backfill import (
    HourlyEnergy,
    drop_implausible_hours,
)
from custom_components.hsem.models.price_profile import PricePair
from custom_components.hsem.models.what_if_history import RecordedDay
from custom_components.hsem.planner.hindsight_scoring import SiteLimits

#: Why a day has no comparison: it has no measured record, and the price
#: history lacks a real price for at least one hour of it.
REASON_PRICE_INCOMPLETE = "price_incomplete"

#: Why a day has no comparison: it has no measured record and its prices are
#: complete, but the statistics lack the house load of at least one hour, or,
#: where a PV source is configured, have no PV hour or a gap between the
#: day's first and last PV hour.
REASON_STATISTICS_INCOMPLETE = "statistics_incomplete"

_HOUR = timedelta(hours=1)
_SLOT_MINUTES = 60


def local_day_hours(day: str, zone: tzinfo | None) -> tuple[datetime, ...]:
    """Return the UTC starts of the hours of a local day.

    23 hours on the day the clocks go forward and 25 on the day they go back.

    Args:
        day: The local day as an ISO date.
        zone: The site's time zone.
    """
    first = date.fromisoformat(day)
    start = datetime.combine(first, time(0), tzinfo=zone).astimezone(UTC)
    end = datetime.combine(first + timedelta(days=1), time(0), tzinfo=zone).astimezone(
        UTC
    )
    return tuple(start + index * _HOUR for index in range((end - start) // _HOUR))


def priced_days(
    days: Iterable[str], zone: tzinfo | None, prices: Collection[datetime]
) -> dict[str, tuple[datetime, ...]]:
    """Return the days that have a real price for every hour.

    Args:
        days: Local days as ISO dates.
        zone: The site's time zone.
        prices: The UTC hour starts a real price is held for.

    Returns:
        The hours of each fully priced day, by day.
    """
    priced: dict[str, tuple[datetime, ...]] = {}
    for day in days:
        hours = local_day_hours(day, zone)
        if hours and all(hour in prices for hour in hours):
            priced[day] = hours
    return priced


def rebuild_day(
    day: str,
    hours: tuple[datetime, ...],
    hourly: HourlyEnergy,
    prices: Mapping[datetime, PricePair],
    site: SiteLimits,
    *,
    pv_expected: bool,
) -> RecordedDay | None:
    """Return a day rebuilt from hourly statistics, or ``None`` when it has gaps.

    Args:
        day: The local day as an ISO date.
        hours: The UTC starts of the day's hours, each with a price.
        hourly: The hourly energy read from the statistics.
        prices: Real import and export price by UTC hour start.
        site: The installation's limits as configured today.
        pv_expected: Whether a PV source is configured.  The day then needs
            at least one PV hour and none missing between its first and last
            one; the hours outside them are hours without production.
            Without a PV source every hour is.

    Returns:
        The day, marked as rebuilt from statistics, or ``None`` when an hour
        of it has no house load, or its PV statistics are absent or have a
        gap where PV is expected.
    """
    if any(hour not in hourly.house_kwh for hour in hours):
        return None
    observed = [index for index, hour in enumerate(hours) if hour in hourly.pv_kwh]
    if pv_expected and (
        not observed or len(observed) != observed[-1] - observed[0] + 1
    ):
        return None
    pv = tuple(
        None if hour not in hourly.pv_kwh else max(hourly.pv_kwh[hour], 0.0)
        for hour in hours
    )
    net = [
        max(hourly.house_kwh[hour], 0.0) - (produced or 0.0)
        for hour, produced in zip(hours, pv, strict=True)
    ]
    return RecordedDay(
        day=day,
        start=hours[0],
        slot_minutes=_SLOT_MINUTES,
        rated_kwh=site.rated_kwh,
        charge_efficiency=site.charge_efficiency,
        discharge_efficiency=site.discharge_efficiency,
        soc_pct=(site.min_soc_pct,) * (len(hours) + 1),
        grid_import_kwh=tuple(max(energy, 0.0) for energy in net),
        grid_export_kwh=tuple(max(-energy, 0.0) for energy in net),
        import_price=tuple(prices[hour][0] for hour in hours),
        export_price=tuple(prices[hour][1] for hour in hours),
        pv_kwh=pv,
        from_statistics=True,
    )


def rebuild_days(
    priced: Mapping[str, tuple[datetime, ...]],
    hourly: HourlyEnergy,
    prices: Mapping[datetime, PricePair],
    site: SiteLimits,
    *,
    pv_expected: bool,
) -> tuple[dict[str, RecordedDay], dict[str, str]]:
    """Rebuild every fully priced day the statistics cover.

    Hours above the meter limit are dropped first, as the energy backfill
    does (:func:`~custom_components.hsem.models.energy_backfill.drop_implausible_hours`).

    Args:
        priced: The hours of each fully priced day, from :func:`priced_days`.
        hourly: The hourly energy read from the statistics.
        prices: Real import and export price by UTC hour start.
        site: The installation's limits as configured today.
        pv_expected: Whether a PV source is configured.

    Returns:
        The rebuilt days by ISO date, and
        :data:`REASON_STATISTICS_INCOMPLETE` for each day that could not be
        rebuilt.
    """
    hourly, _dropped = drop_implausible_hours(hourly)
    rebuilt: dict[str, RecordedDay] = {}
    reasons: dict[str, str] = {}
    for day, hours in priced.items():
        recorded = rebuild_day(
            day, hours, hourly, prices, site, pv_expected=pv_expected
        )
        if recorded is None:
            reasons[day] = REASON_STATISTICS_INCOMPLETE
        else:
            rebuilt[day] = recorded
    return rebuilt, reasons
