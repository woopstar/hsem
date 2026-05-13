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
        avg_house_consumption: Weighted spike-aware consumption estimate (kWh).
        avg_house_consumption_1d: 1-day window contribution (kWh).
        avg_house_consumption_3d: 3-day window contribution (kWh).
        avg_house_consumption_7d: 7-day window contribution (kWh).
        avg_house_consumption_14d: 14-day window contribution (kWh).
        solcast_pv_estimate: Forecast PV production (kWh).
        estimated_net_consumption: avg_consumption - pv_estimate (kWh).
        estimated_cost: Estimated grid cost for the slot (local currency).
        batteries_charged: Energy scheduled to be charged into battery (kWh).
        batteries_discharged: Energy drawn from battery by the SoC simulation (kWh).
        estimated_battery_capacity: Remaining usable battery energy above the
            discharge floor at the end of the slot (kWh).
        estimated_battery_soc: Simulated absolute battery SoC (0-100 %) at the
            end of the slot, relative to the rated capacity.  Populated by
            :func:`~planner.soc_simulation.simulate_soc` and suitable for
            plotting in an Apex chart time-series.
        grid_import_kwh: Energy imported from the grid during this slot (kWh).
        grid_export_kwh: Energy exported to the grid during this slot (kWh).
    """

    avg_house_consumption: float
    avg_house_consumption_1d: float
    avg_house_consumption_3d: float
    avg_house_consumption_7d: float
    avg_house_consumption_14d: float
    batteries_charged: float
    batteries_discharged: float
    end: datetime
    estimated_battery_capacity: float
    estimated_battery_soc: float
    estimated_cost: float
    estimated_net_consumption: float
    export_price: float
    grid_export_kwh: float
    grid_import_kwh: float
    import_price: float
    recommendation: Any | None
    solcast_pv_estimate: float
    start: datetime
