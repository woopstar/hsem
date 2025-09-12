from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class HourlyRecommendation:
    avg_house_consumption: float
    batteries_charged: float
    end: datetime
    estimated_battery_capacity: float
    estimated_battery_soc: int
    estimated_net_consumption: float
    export_price: float
    import_price: float
    recommendation: Any | None
    solcast_pv_estimate: float
    start: datetime
