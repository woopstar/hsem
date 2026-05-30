"""Working mode enumeration for Huawei Luna2000 inverters.

Defines the three inverter operating modes that HSEM can switch between.
"""

from enum import Enum


class WorkingModes(Enum):
    """Huawei Luna2000 inverter working modes."""

    TimeOfUse = "time_of_use_luna2000"
    """Time-of-Use mode: battery charges/discharges according to a TOU schedule."""

    MaximizeSelfConsumption = "maximise_self_consumption"
    """Maximise self-consumption: battery prioritises powering the home over export."""

    FullyFedToGrid = "fully_fed_to_grid"
    """Fully fed to grid: all solar production is exported to the grid."""
