from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class HourlyRecommendation:
    avg_house_consumption: float
    avg_house_consumption_1d: float
    avg_house_consumption_3d: float
    avg_house_consumption_7d: float
    avg_house_consumption_14d: float
    batteries_charged: float
    end: datetime
    estimated_battery_capacity: float
    estimated_battery_soc: float
    estimated_cost: float
    estimated_net_consumption: float
    export_price: float
    import_price: float
    recommendation: Any | None
    solcast_pv_estimate: float
    start: datetime
