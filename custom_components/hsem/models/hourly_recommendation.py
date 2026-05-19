"""Dataclass representing a single planning slot recommendation."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class HourlyRecommendation:
    """A single time-slot planning decision produced by the HSEM planner.

    All numeric energy fields are in kWh for the slot duration.

    Attributes:
        start: Timezone-aware start of the slot.
        end: Timezone-aware end of the slot.
        recommendation: Working-mode recommendation string (or None).
        import_price: Spot import price (local currency/kWh).
        export_price: Spot export price (local currency/kWh).
        avg_house_consumption_kwh: Weighted spike-aware consumption estimate (kWh).
        avg_house_consumption_1d_kwh: 1-day window contribution (kWh).
        avg_house_consumption_3d_kwh: 3-day window contribution (kWh).
        avg_house_consumption_7d_kwh: 7-day window contribution (kWh).
        avg_house_consumption_14d_kwh: 14-day window contribution (kWh).
        solcast_pv_estimate_kwh: Forecast PV production (kWh).
        estimated_net_consumption_kwh: avg_consumption + ev_planned_load_kwh - pv_estimate (kWh).
        ev_planned_load_kwh: Extra EV AC load added to net consumption (kWh, ≥ 0).
            Combined injected load from primary and second EV.  Zero when EV
            planned load integration is disabled, the EV is not scheduled to
            charge, or ``base_load_includes_ev=True`` (EV already in base load).
        ev_accounted_load_kwh: EV AC load already included in the house
            consumption sensor (kWh, ≥ 0).  Non-zero only when
            ``base_load_includes_ev=True``.  Not added to net consumption.
        ev_total_planned_load_kwh: Total EV AC load planned for this slot
            (kWh, ≥ 0).  Equals ``ev_planned_load_kwh + ev_accounted_load_kwh``.
            Use this for diagnostics and UI — it is non-zero whenever EV
            charging is planned regardless of the ``base_load_includes_ev`` flag.
        estimated_cost_currency: Estimated grid cost for the slot (local currency).
        batteries_charged_kwh: Energy scheduled to be charged into battery (kWh).
        batteries_discharged_kwh: Energy drawn from battery by the SoC simulation (kWh).
        estimated_battery_capacity_kwh: Remaining usable battery energy above the
            discharge floor at the end of the slot (kWh).
        estimated_battery_soc_pct: Simulated absolute battery SoC (0-100 %) at the
            end of the slot, relative to the rated capacity.  Populated by
            :func:`~planner.soc_simulation.simulate_soc` and suitable for
            plotting in an Apex chart time-series.
        grid_import_kwh: Energy imported from the grid during this slot (kWh).
        grid_export_kwh: Energy exported to the grid during this slot (kWh).
    """

    start: datetime
    end: datetime
    avg_house_consumption_kwh: float
    avg_house_consumption_1d_kwh: float
    avg_house_consumption_3d_kwh: float
    avg_house_consumption_7d_kwh: float
    avg_house_consumption_14d_kwh: float
    batteries_charged_kwh: float
    batteries_discharged_kwh: float
    estimated_battery_capacity_kwh: float
    estimated_battery_soc_pct: float
    estimated_cost_currency: float
    estimated_net_consumption_kwh: float
    export_price: float
    grid_export_kwh: float
    grid_import_kwh: float
    import_price: float
    recommendation: Any | None
    solcast_pv_estimate_kwh: float
    ev_planned_load_kwh: float = 0.0
    ev_accounted_load_kwh: float = 0.0
    ev_total_planned_load_kwh: float = 0.0
