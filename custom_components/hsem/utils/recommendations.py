"""
This module defines the `Recommendations` enumeration for various energy management recommendations.

Classes:
    Recommendations (Enum): Represents different working modes or strategies for energy management.
"""

from enum import Enum


class Recommendations(Enum):
    """
    An enumeration representing various recommendations for energy management.
    """

    BatteriesChargeGrid = "batteries_charge_grid"
    BatteriesChargeSolar = "batteries_charge_solar"
    BatteriesDischargeMode = "batteries_discharge_mode"
    EVSmartCharging = "ev_smart_charging"
    ForceBatteriesDischarge = "force_batteries_discharge"
    ForceExport = "force_export"
    FullyFedToGrid = "fully_fed_to_grid"
    MaximizeSelfConsumption = "maximise_self_consumption"
    MissingInputEntities = "missing_input_entities"
    TimeOfUse = "time_of_use_luna2000"
