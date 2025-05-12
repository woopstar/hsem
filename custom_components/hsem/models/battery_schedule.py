"""
This module defines the BatterySchedule class, which encapsulates the battery schedule.

Classes:
    BatterySchedule: Represents the battery schedule.
"""

from dataclasses import dataclass
from datetime import time


@dataclass
class BatterySchedule:
    enabled: bool
    start: time
    end: time
    avg_import_price: float
    needed_batteries_capacity: float
    needed_batteries_capacity_cost: float
    min_price_difference: float
