"""Shared fixtures for HSEM planner unit tests.

This module provides factory functions that return fully-populated
:class:`~custom_components.hsem.models.planner_inputs.PlannerInput` objects
representing common scenario archetypes.  All fixtures are pure-Python and
carry no Home Assistant dependencies.

Usage example
-------------
>>> from tests.planner.fixtures import make_summer_day_input
>>> inp = make_summer_day_input()
>>> from custom_components.hsem.planner import run_planner
>>> output = run_planner(inp)
>>> assert output.slots  # 24 slots for a 24-hour, 60-min horizon
"""

from __future__ import annotations

from datetime import time

from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot

_DEFAULT_SUMMER_ISO = "2024-06-15T00:00:00+02:00"

# ---------------------------------------------------------------------------
# Base 24-hour time-series helpers
# ---------------------------------------------------------------------------

# Danish-style spot prices (EUR/kWh, roughly realistic day-ahead pattern)
# Index 0 = 00:00-01:00, index 23 = 23:00-24:00
_SPOT_PRICES_SUMMER = [
    # 00-06: cheap overnight
    0.08,
    0.06,
    0.05,
    0.05,
    0.06,
    0.09,
    # 06-10: morning ramp
    0.15,
    0.22,
    0.26,
    0.24,
    # 10-15: mid-day dip (solar depresses price)
    0.12,
    0.08,
    0.06,
    0.07,
    0.10,
    # 15-21: evening peak
    0.25,
    0.30,
    0.32,
    0.29,
    0.24,
    # 21-24: late evening taper
    0.18,
    0.14,
    0.11,
    0.09,
]

_SPOT_PRICES_WINTER = [
    # 00-06: cheap overnight
    0.10,
    0.08,
    0.07,
    0.07,
    0.08,
    0.12,
    # 06-10: morning peak
    0.28,
    0.38,
    0.42,
    0.36,
    # 10-15: mid-day moderate
    0.22,
    0.18,
    0.16,
    0.17,
    0.20,
    # 15-21: evening peak (no solar to dampen it)
    0.38,
    0.45,
    0.48,
    0.44,
    0.35,
    # 21-24: late taper
    0.25,
    0.20,
    0.15,
    0.12,
]


# Export prices are import minus ~20% grid tariff
def _export_from_import(import_price: float) -> float:
    return round(max(import_price - 0.02, 0.0), 4)


# Typical summer PV production per hour (kWh, south-facing 8 kWp system)
_SOLCAST_SUMMER = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,  # 00-06: night
    0.1,
    0.4,
    1.2,
    2.5,  # 06-10: rising
    3.8,
    5.0,
    5.5,
    5.2,
    4.8,  # 10-15: peak
    3.8,
    2.5,
    1.5,
    0.6,
    0.1,  # 15-20: setting
    0.0,
    0.0,
    0.0,
    0.0,  # 20-24: night
]

# Winter: much less solar
_SOLCAST_WINTER = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.1,
    0.4,
    0.9,
    1.3,
    1.6,
    1.7,
    1.5,
    1.2,
    0.7,
    0.3,
    0.1,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
]

# Typical house consumption per hour (kWh)
# Morning spike, lunch slight rise, evening peak
_HOUSE_CONSUMPTION = [
    0.4,
    0.3,
    0.3,
    0.3,
    0.3,
    0.4,  # 00-06: base load
    0.6,
    0.9,
    0.8,
    0.6,  # 06-10: morning
    0.5,
    0.5,
    0.6,
    0.5,
    0.5,  # 10-15: day
    0.6,
    0.8,
    1.1,
    1.2,
    1.0,  # 15-20: evening
    0.8,
    0.6,
    0.5,
    0.4,  # 20-24: wind-down
]


def _make_price_points(import_prices: list[float]) -> list[PricePoint]:
    return [
        PricePoint(hour=h, import_price=p, export_price=_export_from_import(p))
        for h, p in enumerate(import_prices)
    ]


def _make_solcast_slots(productions: list[float]) -> list[SolcastSlot]:
    return [SolcastSlot(hour=h, pv_estimate=kwh) for h, kwh in enumerate(productions)]


def _make_consumption_averages(
    consumption: list[float],
) -> list[HourlyConsumptionAverage]:
    """Return 24 HourlyConsumptionAverage objects using the same values for
    all four historical windows (1d/3d/7d/14d).  This produces a stable
    weighted average that matches the raw input, making test assertions easy.
    """
    return [
        HourlyConsumptionAverage(
            hour=h,
            avg_1d=kwh,
            avg_3d=kwh,
            avg_7d=kwh,
            avg_14d=kwh,
        )
        for h, kwh in enumerate(consumption)
    ]


# ---------------------------------------------------------------------------
# Default battery schedules
# ---------------------------------------------------------------------------


def _default_schedules() -> list[BatteryScheduleInput]:
    """Return the two default battery charge/discharge schedules.

    - Schedule 1: discharge 07:00–09:00 (morning peak)
    - Schedule 2: discharge 17:00–21:00 (evening peak)
    """
    return [
        BatteryScheduleInput(
            enabled=True,
            start=time(7, 0),
            end=time(9, 0),
        ),
        BatteryScheduleInput(
            enabled=True,
            start=time(17, 0),
            end=time(21, 0),
        ),
    ]


# ---------------------------------------------------------------------------
# Public fixture factories
# ---------------------------------------------------------------------------


def make_summer_day_input(
    *,
    now_iso: str = _DEFAULT_SUMMER_ISO,
    battery_soc_pct: float = 50.0,
    battery_rated_capacity_kwh: float = 10.0,
    battery_end_of_discharge_soc_pct: float = 10.0,
    battery_max_charge_power_w: float = 5000.0,
    interval_minutes: int = 60,
    interval_length_hours: int = 24,
    schedules: list[BatteryScheduleInput] | None = None,
    months_winter: list[int] | None = None,
) -> PlannerInput:
    """Return a 24-hour summer planning input.

    The fixture represents a clear summer day with high PV production,
    low mid-day prices, and high evening prices.  Battery starts at 50 % SoC.

    Args:
        now_iso: Planning timestamp (ISO-8601, timezone-aware).
        battery_soc_pct: Initial battery state-of-charge (0-100 %).
        battery_rated_capacity_kwh: Nameplate battery capacity in kWh.
        battery_end_of_discharge_soc_pct: End-of-discharge reserve (0-100 %).
        battery_max_charge_power_w: Maximum charge power in Watts.
        onversion loss (0-100 %).
        interval_minutes: Slot width in minutes (15 or 60).
        interval_length_hours: Planning horizon in hours (e.g. 24 or 48).
        schedules: Override the default discharge schedules.
        months_winter: Override the list of winter months.

    Returns:
        Fully populated :class:`PlannerInput`.
    """
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=interval_length_hours,
        # battery
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=battery_rated_capacity_kwh,
        battery_end_of_discharge_soc_pct=battery_end_of_discharge_soc_pct,
        battery_max_charge_power_w=battery_max_charge_power_w,
        battery_purchase_price=10_000.0,
        battery_expected_cycles=6000,
        # weights
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        # time-series
        consumption_averages=_make_consumption_averages(_HOUSE_CONSUMPTION),
        price_points=_make_price_points(_SPOT_PRICES_SUMMER),
        solcast_slots=_make_solcast_slots(_SOLCAST_SUMMER),
        # schedules
        battery_schedules=schedules if schedules is not None else _default_schedules(),
        # excess export
        excess_export_enabled=True,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        # seasonal
        months_winter=(
            months_winter if months_winter is not None else [1, 2, 3, 4, 10, 11, 12]
        ),
        house_power_includes_ev=True,
        is_read_only=True,
        time_discount_rate=1.0,
    )


def make_winter_day_input(
    *,
    now_iso: str = "2024-01-15T00:00:00+01:00",
    battery_soc_pct: float = 80.0,
    battery_rated_capacity_kwh: float = 10.0,
    battery_end_of_discharge_soc_pct: float = 10.0,
    battery_max_charge_power_w: float = 5000.0,
    interval_minutes: int = 60,
    interval_length_hours: int = 24,
    schedules: list[BatteryScheduleInput] | None = None,
    months_winter: list[int] | None = None,
) -> PlannerInput:
    """Return a 24-hour winter planning input.

    The fixture represents a winter day with minimal PV production, high
    morning and evening prices, and a nearly-full battery.

    Args:
        now_iso: Planning timestamp (ISO-8601, timezone-aware).
        battery_soc_pct: Initial battery state-of-charge (0-100 %).
        battery_rated_capacity_kwh: Nameplate battery capacity in kWh.
        battery_end_of_discharge_soc_pct: End-of-discharge reserve (0-100 %).
        battery_max_charge_power_w: Maximum charge power in Watts.
        onversion loss (0-100 %).
        interval_minutes: Slot width in minutes (15 or 60).
        interval_length_hours: Planning horizon in hours (e.g. 24 or 48).
        schedules: Override the default discharge schedules.
        months_winter: Override the list of winter months.

    Returns:
        Fully populated :class:`PlannerInput`.
    """
    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=interval_length_hours,
        # battery
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=battery_rated_capacity_kwh,
        battery_end_of_discharge_soc_pct=battery_end_of_discharge_soc_pct,
        battery_max_charge_power_w=battery_max_charge_power_w,
        battery_purchase_price=10_000.0,
        battery_expected_cycles=6000,
        # weights
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        # time-series
        consumption_averages=_make_consumption_averages(_HOUSE_CONSUMPTION),
        price_points=_make_price_points(_SPOT_PRICES_WINTER),
        solcast_slots=_make_solcast_slots(_SOLCAST_WINTER),
        # schedules
        battery_schedules=schedules if schedules is not None else _default_schedules(),
        # excess export disabled in winter (no meaningful solar surplus)
        excess_export_enabled=False,
        excess_export_discharge_buffer_pct=10.0,
        excess_export_price_threshold=0.10,
        # seasonal
        months_winter=(
            months_winter if months_winter is not None else [1, 2, 3, 4, 10, 11, 12]
        ),
        house_power_includes_ev=True,
        is_read_only=True,
        time_discount_rate=1.0,
    )


def make_flat_price_input(
    *,
    now_iso: str = _DEFAULT_SUMMER_ISO,
    import_price: float = 0.20,
    export_price: float = 0.05,
    battery_soc_pct: float = 0.0,
    interval_minutes: int = 60,
    interval_length_hours: int = 24,
) -> PlannerInput:
    """Return a 24-hour input with constant prices and no PV production.

    Useful for testing charge/discharge scheduling in isolation without
    price signal noise.

    Args:
        now_iso: Planning timestamp (ISO-8601, timezone-aware).
        import_price: Flat import price (local currency/kWh).
        export_price: Flat export price (local currency/kWh).
        battery_soc_pct: Initial battery state-of-charge (0-100 %).
        interval_minutes: Slot width in minutes (15 or 60).
        interval_length_hours: Planning horizon in hours.

    Returns:
        Fully populated :class:`PlannerInput` with flat prices and no PV.
    """
    flat_prices = [
        PricePoint(hour=h, import_price=import_price, export_price=export_price)
        for h in range(24)
    ]
    no_solar = [SolcastSlot(hour=h, pv_estimate=0.0) for h in range(24)]
    consumption = _make_consumption_averages(_HOUSE_CONSUMPTION)

    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=interval_length_hours,
        battery_soc_pct=battery_soc_pct,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=0.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=consumption,
        price_points=flat_prices,
        solcast_slots=no_solar,
        battery_schedules=_default_schedules(),
        excess_export_enabled=False,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        is_read_only=True,
        time_discount_rate=1.0,
    )


def make_negative_price_input(
    *,
    now_iso: str = _DEFAULT_SUMMER_ISO,
    negative_hours: list[int] | None = None,
    interval_minutes: int = 60,
) -> PlannerInput:
    """Return a 24-hour summer input with configurable negative price hours.

    Negative import prices should trigger ``BatteriesChargeGrid``
    recommendations.

    Args:
        now_iso: Planning timestamp (ISO-8601, timezone-aware).
        negative_hours: Hours (0-23) at which the import price is negative.
            Defaults to ``[1, 2, 3]``.
        interval_minutes: Slot width.

    Returns:
        Fully populated :class:`PlannerInput`.
    """
    if negative_hours is None:
        negative_hours = [1, 2, 3]

    prices = list(_SPOT_PRICES_SUMMER)
    for h in negative_hours:
        prices[h] = -0.05  # paid to consume

    price_points = [
        PricePoint(
            hour=h, import_price=p, export_price=_export_from_import(max(p, 0.0))
        )
        for h, p in enumerate(prices)
    ]

    return PlannerInput(
        now_iso=now_iso,
        interval_minutes=interval_minutes,
        interval_length_hours=24,
        battery_soc_pct=0.0,
        battery_rated_capacity_kwh=10.0,
        battery_end_of_discharge_soc_pct=10.0,
        battery_max_charge_power_w=5000.0,
        battery_purchase_price=0.0,
        battery_expected_cycles=6000,
        weight_1d=25,
        weight_3d=30,
        weight_7d=30,
        weight_14d=15,
        consumption_averages=_make_consumption_averages(_HOUSE_CONSUMPTION),
        price_points=price_points,
        solcast_slots=_make_solcast_slots(_SOLCAST_SUMMER),
        battery_schedules=_default_schedules(),
        excess_export_enabled=False,
        months_winter=[1, 2, 3, 4, 10, 11, 12],
        is_read_only=True,
        time_discount_rate=1.0,
    )
