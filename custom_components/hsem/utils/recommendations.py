"""
This module defines the `Recommendations` enumeration for different recommendations.

Classes:
    Recommendations (Enum): An enumeration representing various recommendations.

Members:
    TimeOfUse: Represents the "time_of_use_luna2000" working mode.
    MaximizeSelfConsumption: Represents the "maximise_self_consumption" working mode.
    FullyFedToGrid: Represents the "fully_fed_to_grid" working mode.
    ForceBatteriesCharge: Represents a mode to "force_batteries_charge".
    ForceBatteriesDischarge: Represents a mode to "force_batteries_discharge".
    EVSmartCharging: Represents a mode to enable "ev_smart_charging".
    ForceExport: Represents a mode to "force_export" energy to the grid.
"""

from enum import Enum


class Recommendations(Enum):
    TimeOfUse = "time_of_use_luna2000"
    MaximizeSelfConsumption = "maximise_self_consumption"
    FullyFedToGrid = "fully_fed_to_grid"
    ForceBatteriesCharge = "force_batteries_charge"
    ForceBatteriesDischarge = "force_batteries_discharge"
    EVSmartCharging = "ev_smart_charging"
    ForceExport = "force_export"
