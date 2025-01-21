"""
This module defines the `WorkingModes` enumeration for different working modes.

Classes:
    WorkingModes (Enum): An enumeration representing various working modes.

"""

from enum import Enum


class WorkingModes(Enum):
    TimeOfUse = "time_of_use_luna2000"
    MaximizeSelfConsumption = "maximise_self_consumption"
    FullyFedToGrid = "fully_fed_to_grid"
