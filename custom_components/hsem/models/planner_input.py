"""Dataclass for the complete set of inputs required to run the HSEM planner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.models.battery_schedule_input import BatteryScheduleInput
from custom_components.hsem.models.hourly_consumption_average import (
    HourlyConsumptionAverage,
)
from custom_components.hsem.models.price_point import PricePoint
from custom_components.hsem.models.solcast_slot import SolcastSlot


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
        battery_charge_efficiency_pct:
            Charge-side efficiency as a percentage (0-100).  Energy stored in
            the battery equals input energy × (charge_efficiency_pct / 100).
            Defaults to 97 % (3 % charge-side loss).
        battery_discharge_efficiency_pct:
            Discharge-side efficiency as a percentage (0-100).  Energy delivered
            to the house equals battery energy removed × (discharge_efficiency_pct / 100).
            Defaults to 97 % (3 % discharge-side loss).
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
    battery_charge_efficiency_pct: float = 97.0
    battery_discharge_efficiency_pct: float = 97.0

    # --- battery economics ---
    battery_purchase_price: float = 0.0
    battery_expected_cycles: int = 6000
    #: Additional per-kWh cost of one charge/discharge cycle (wear / tear).
    #: Added to the min-price-difference guard so the planner only charges
    #: from the grid when the price spread covers loss **and** wear.
    #: 0.0 means no extra guard beyond the depreciation threshold.
    battery_cycle_cost_per_kwh: float = 0.0

    #: Expected battery capacity loss at end-of-life as a percentage (0-100).
    #: LiFePO4 EOL is typically 20 % (80 % retained).  Default 30 % includes
    #: margin for calendar ageing.
    battery_capacity_loss_pct: float = field(
        default_factory=lambda: cast(
            float, DEFAULT_CONFIG_VALUES["hsem_batteries_capacity_loss_pct"]
        )
    )

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

    # --- main fuse / tariff protection ---
    #: Main fuse/breaker rating in amps (0 or None = disabled).  The MILP
    #: optimizer uses this as a soft constraint on total grid import power.
    main_fuse_amps: float | None = None

    # --- seasonal / mode config ---
    months_winter: list[int] = field(default_factory=lambda: [1, 2, 3, 4, 10, 11, 12])
    house_power_includes_ev: bool = True
    is_read_only: bool = False  # False = hardware writes enabled; set True only in dry-run/test scenarios

    #: Live solar production power in Watts from the inverter's input power
    #: sensor.  Injected into the current slot's solcast_pv_estimate_kwh so
    #: the MILP and all candidates use measured (not forecast) PV for the
    #: partially-elapsed slot.
    live_solar_production_w: float = 0.0

    #: Live house consumption power in Watts from the house power meter.
    #: Injected into the current slot's avg_house_consumption_kwh so the
    #: MILP and all candidates use measured (not forecast) load for the
    #: partially-elapsed slot.
    live_house_consumption_w: float = 0.0

    # --- time discount for selector score ---
    #: Per-hour exponential discount factor applied to the selector score
    #: (not to total_cost).  A value of 1.0 disables the discount entirely.
    #: Default 0.995 means a saving 48 hours from now is worth ~79% of a
    #: saving right now in the selector's eyes.
    time_discount_rate: float = 0.995

    # --- EV planned load integration — primary EV (optional, disabled by default) ---
    #: When True, the primary EV planned load feature is active.
    ev_planned_load_enabled: bool = False
    ev_planned_load_connected: bool = False
    ev_planned_load_smart_charging_enabled: bool = True
    ev_planned_load_current_soc_pct: float = 0.0
    ev_planned_load_target_soc_pct: float = 80.0
    ev_planned_load_battery_capacity_kwh: float = 0.0
    ev_planned_load_charger_power_kw: float = 0.0
    ev_planned_load_charger_efficiency_pct: float = 100.0
    ev_planned_load_charger_min_power_w: float = 1380.0
    ev_planned_load_deadline: datetime | None = None
    ev_planned_load_base_load_includes_ev: bool = False
    #: When True, the EV may continue charging past its target SoC using
    #: surplus PV that would otherwise be curtailed (e.g. battery full,
    #: negative export prices).
    ev_planned_allow_charge_past_target_soc: bool = False

    # --- EV planned load integration — second EV (optional, disabled by default) ---
    #: When True, the second EV planned load feature is active.
    ev_second_planned_load_enabled: bool = False
    ev_second_planned_load_connected: bool = False
    ev_second_planned_load_smart_charging_enabled: bool = True
    ev_second_planned_load_current_soc_pct: float = 0.0
    ev_second_planned_load_target_soc_pct: float = 80.0
    ev_second_planned_load_battery_capacity_kwh: float = 0.0
    ev_second_planned_load_charger_power_kw: float = 0.0
    ev_second_planned_load_charger_efficiency_pct: float = 100.0
    ev_second_planned_load_charger_min_power_w: float = 1380.0
    ev_second_planned_load_deadline: datetime | None = None
    ev_second_planned_load_base_load_includes_ev: bool = False
    #: Same as ev_planned_allow_charge_past_target_soc, for the second EV.
    ev_second_allow_charge_past_target_soc: bool = False

    # --- planner hysteresis — keep the active plan unless the new plan
    # is materially better (anti-flapping, issue #372). ---
    #: When True, hysteresis is active.  The previous winner's strategy
    #: is kept unless a new candidate improves score by more than the
    #: configured threshold.
    planner_hysteresis_enabled: bool = True
    #: Absolute hysteresis threshold in local currency.  The previous plan
    #: is kept unless the new winner's score is lower (better) by at least
    #: this amount.  0.0 disables the absolute threshold.
    planner_hysteresis_absolute: float = 0.0
    #: Percentage hysteresis threshold.  The previous plan is kept unless
    #: the new winner's score is at least this percentage lower (better).
    #: 0.0 disables the percentage threshold.
    planner_hysteresis_percentage: float = 5.0
    #: Window-level hysteresis — minimum hold time (minutes) before
    #: allowing a charge↔discharge transition on adjacent slots.
    #: 0 disables the feature.
    planner_window_hysteresis_minutes: int = 0
    #: Name of the winning candidate from the previous planner run.
    #: ``None`` on the first run (no active plan to preserve).
    previous_winner_name: str | None = None
    #: Score of the winning candidate from the previous planner run.
    #: 0.0 when there is no previous run.
    previous_winner_score: float = 0.0

    # --- optional extra context that tests may inspect ---
    extra: dict[str, Any] = field(default_factory=dict)

    # --- session-aware EV demand (issue #615) ---
    #: Current session charge power for the primary EV in kW, or None when
    #: the EV is not actively charging.  When set, the MILP treats the first
    #: 2 hours of EV slots as certain demand at this power level.
    ev_session_charge_kw: float | None = None
    #: Same as above for the second EV.
    ev_second_session_charge_kw: float | None = None

    # --- solar forecast auto-correction (issue #602) ---
    #: Optional :class:`~custom_components.hsem.utils.solar_corrector.SolarForecastCorrector`
    #: instance for per-hour PV forecast accuracy correction.  When ``None`` the
    #: raw Solcast forecast is used unchanged.
    solar_corrector: Any = field(default=None, repr=False)

    # --- dynamic discharge floor (issue #600) ---
    #: Effective discharge floor SoC percentage computed by
    #: :class:`~custom_components.hsem.utils.dynamic_floor.DynamicDischargeFloor`.
    #: When ``None`` the feature is disabled and the configured minimum is used.
    #: When set, the planner's discharge/export logic uses this floor instead
    #: of the raw ``battery_end_of_discharge_soc_pct``.
    dynamic_discharge_floor_pct: float | None = None
