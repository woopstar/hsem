"""
This module defines the `WorkingModes` enumeration for different working modes.

Classes:
    WorkingModes (Enum): An enumeration representing various working modes.

Members:
    TimeOfUse: Represents the "time_of_use_luna2000" working mode.
    MaximizeSelfConsumption: Represents the "maximise_self_consumption" working mode.
    FullyFedToGrid: Represents the "fully_fed_to_grid" working mode.
"""

from enum import Enum


class WorkingModes(Enum):
    TimeOfUse = "time_of_use_luna2000"
    MaximizeSelfConsumption = "maximise_self_consumption"
    FullyFedToGrid = "fully_fed_to_grid"
