"""This module defines the `Recommendations` enumeration for various energy management recommendations.

Classes:
    Recommendations (Enum): Represents different working modes or strategies for energy management.
"""

from enum import Enum


class Recommendations(Enum):
    """An enumeration representing various recommendations for energy management."""

    TimePassed = "time_passed"
    BatteriesChargeGrid = "batteries_charge_grid"
    BatteriesChargeSolar = "batteries_charge_solar"
    BatteriesDischargeMode = "batteries_discharge_mode"
    BatteriesWaitMode = "batteries_wait_mode"
    EVSmartCharging = "ev_smart_charging"
    ForceBatteriesDischarge = "force_batteries_discharge"
    ForceExport = "force_export"
    MissingInputEntities = "missing_input_entities"


# Canonical set of all discharge-type recommendations.
# Import these instead of re-defining locally.
DISCHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    }
)

# Canonical set of all charge-type recommendations.
# Import these instead of re-defining locally.
CHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
        Recommendations.EVSmartCharging.value,
    }
)
