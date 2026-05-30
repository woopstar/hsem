"""Dataclass representing a battery charge/discharge schedule window.

A battery schedule defines a time window during which the battery should
charge or discharge, along with the economic parameters that drive the
decision (average import price and required capacity).
"""

from dataclasses import dataclass
from datetime import time


@dataclass
class BatterySchedule:
    """A single battery charge/discharge schedule window.

    Holds the enabled state, time boundaries, and economic parameters
    (average import price, needed capacity, and associated cost) for one
    battery schedule window.
    """

    enabled: bool
    start: time
    end: time
    avg_import_price: float
    needed_batteries_capacity: float
    needed_batteries_capacity_cost: float
