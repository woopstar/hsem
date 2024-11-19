"""
This module defines the `Recommendations` enumeration for various energy management recommendations.

Classes:
    Recommendations (Enum): Represents different working modes or strategies for energy management.
"""

from enum import Enum

class Recommendations(Enum):
    """
    An enumeration representing various recommendations for energy management.

    Attributes:
        TimeOfUse (str): Working mode for "time_of_use_luna2000".
        MaximizeSelfConsumption (str): Working mode for maximizing self-consumption of solar energy.
        FullyFedToGrid (str): Working mode for exporting all energy to the grid.
        ForceBatteriesCharge (str): Forces batteries to charge during low-cost energy periods.
        ForceBatteriesDischarge (str): Forces batteries to discharge to supply energy demand.
        EVSmartCharging (str): Activates smart charging mode for electric vehicles.
        ForceExport (str): Forces energy export to the grid under certain conditions.
    """
    TimeOfUse = "time_of_use_luna2000"
    MaximizeSelfConsumption = "maximise_self_consumption"
    FullyFedToGrid = "fully_fed_to_grid"
    ForceBatteriesCharge = "force_batteries_charge"
    ForceBatteriesDischarge = "force_batteries_discharge"
    EVSmartCharging = "ev_smart_charging"
    ForceExport = "force_export"
