"""Pure-Python dataclasses for HSEM planner inputs.

These dataclasses capture every value that the planner needs to compute
charge/discharge schedules and hourly recommendations.  They carry *no*
Home Assistant dependencies so they can be constructed and inspected in
plain unit tests without a running HA instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any


@dataclass
class HourlyConsumptionAverage:
    """Historical consumption averages for one clock-hour.

    All values are in kWh for the full hour.
    """

    hour: int  # 0-23
    avg_1d: float = 0.0
    avg_3d: float = 0.0
    avg_7d: float = 0.0
    avg_14d: float = 0.0


@dataclass
class PricePoint:
    """An import or export electricity price for a single time slot.

    ``hour`` is the 0-based calendar hour (0-23).
    Prices are in the local currency per kWh (e.g. DKK/kWh).
    """

    hour: int  # 0-23
    import_price: float = 0.0
    export_price: float = 0.0


@dataclass
class SolcastSlot:
    """Forecast PV production estimate for a single time slot.

    ``hour`` is the 0-based calendar hour (0-23).
    ``pv_estimate`` is in kWh for the full slot duration.
    """

    hour: int  # 0-23
    pv_estimate: float = 0.0


@dataclass
class BatteryScheduleInput:
    """Configuration for one charge-into/discharge-from schedule window.

    Mirrors the user-visible battery schedule options from the config flow
    (``batteries_schedule_1/2/3``).
    """

    enabled: bool = False
    start: time = time(0, 0)
    end: time = time(1, 0)
    min_price_difference: float = 0.0


@dataclass
class PlannerInput:
    """Complete set of inputs required to run the HSEM planner.

    All fields are plain Python types with no Home Assistant imports.

    Attributes:
        now_iso:
            ISO-8601 timestamp of the planning moment (e.g.
            ``"2024-06-15T14:00:00+02:00"``).  Must be timezone-aware.
        interval_minutes:
            Planning slot width in minutes.  Typical values: 15 or 60.
        interval_length_hours:
            How far into the future to generate recommendations.  Together
            with ``interval_minutes`` this determines the total number of
            slots: ``slots = (interval_length_hours * 60) // interval_minutes``.
        battery_soc_pct:
            Current battery state-of-charge as a percentage (0-100).
        battery_rated_capacity_kwh:
            Nameplate capacity of the battery pack in kWh.
        battery_end_of_discharge_soc_pct:
            Minimum allowed SoC during discharge (0-100).
        battery_max_soc_pct:
            Maximum allowed SoC during charging (0-100).  Defaults to 100 %
            (no upper restriction beyond nameplate capacity).
        battery_max_charge_power_w:
            Maximum charging power in Watts.
        battery_max_discharge_power_w:
            Maximum discharging power in Watts.  ``None`` means unlimited /
            use the inverter default.
        battery_conversion_loss_pct:
            Round-trip conversion loss as a percentage (0-100).
        battery_purchase_price:
            Purchase price of the battery pack (local currency).  Used for
            depreciation-based threshold calculation.
        battery_expected_cycles:
            Expected total lifetime cycles of the battery.  Used for
            depreciation-based threshold calculation.
        weight_1d:
            Weight (0-100, integer percent) assigned to the 1-day average.
        weight_3d:
            Weight (0-100, integer percent) assigned to the 3-day average.
        weight_7d:
            Weight (0-100, integer percent) assigned to the 7-day average.
        weight_14d:
            Weight (0-100, integer percent) assigned to the 14-day average.
        consumption_averages:
            Per-hour historical averages.  Should cover hours 0-23; missing
            hours default to zero consumption.
        price_points:
            Import / export prices.  Should cover every planned slot; missing
            slots default to zero.
        solcast_slots:
            PV production forecast.  Should cover every planned slot; missing
            slots default to zero.
        battery_schedules:
            Up to three charge/discharge schedule windows.
        excess_export_enabled:
            Whether the excess-export feature is active.
        excess_export_discharge_buffer_pct:
            Safety buffer kept in the battery before forced export (0-100).
        excess_export_price_threshold:
            Minimum export price required to trigger forced export.
        export_min_price:
            Minimum export price for grid power control (below this the
            inverter export is throttled to zero).
        months_winter:
            Month numbers (1-12) classified as winter.
        house_power_includes_ev:
            Whether the house-consumption sensor already includes EV charger
            power.  Affects net-consumption calculation.
        is_read_only:
            When ``True`` the planner skips writing to the inverter.  Useful
            for dry-run/test scenarios.
    """

    # --- temporal context ---
    now_iso: str = "2024-06-15T00:00:00+02:00"
    interval_minutes: int = 60
    interval_length_hours: int = 24

    # --- battery hardware ---
    battery_soc_pct: float = 50.0
    battery_rated_capacity_kwh: float = 10.0
    battery_end_of_discharge_soc_pct: float = 10.0
    battery_max_soc_pct: float = 100.0
    battery_max_charge_power_w: float = 5000.0
    battery_max_discharge_power_w: float | None = None
    battery_conversion_loss_pct: float = 10.0

    # --- battery economics ---
    battery_purchase_price: float = 0.0
    battery_expected_cycles: int = 6000

    # --- consumption weights (must sum to 100) ---
    weight_1d: int = 25
    weight_3d: int = 30
    weight_7d: int = 30
    weight_14d: int = 15

    # --- time-series inputs ---
    consumption_averages: list[HourlyConsumptionAverage] = field(default_factory=list)
    price_points: list[PricePoint] = field(default_factory=list)
    solcast_slots: list[SolcastSlot] = field(default_factory=list)

    # --- discharge / charge schedules ---
    battery_schedules: list[BatteryScheduleInput] = field(default_factory=list)

    # --- excess export ---
    excess_export_enabled: bool = False
    excess_export_discharge_buffer_pct: float = 10.0
    excess_export_price_threshold: float = 0.10

    # --- grid export control ---
    export_min_price: float = 0.0

    # --- seasonal / mode config ---
    months_winter: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 10, 11, 12])
    house_power_includes_ev: bool = True
    is_read_only: bool = False  # False = hardware writes enabled; set True only in dry-run/test scenarios

    # --- optional extra context that tests may inspect ---
    extra: dict[str, Any] = field(default_factory=dict)
