"""Recommendations enumeration for HSEM planner.

Defines the canonical set of battery/house/grid operating modes with
clear semantics for the SoC simulation and hardware applier.

Usage
-----
>>> from custom_components.hsem.utils.recommendations import Recommendations
>>> slot.recommendation = Recommendations.BatteriesDischargeMode.value
"""

from enum import Enum


class Recommendations(Enum):
    """Battery operating modes — what the battery, house, and grid should do.

    Each mode defines three simultaneous actions:
    - **Battery**: charge, discharge, or hold
    - **House**: covered by battery vs imported from grid
    - **Grid**: import, export, or idle
    """

    # ------------------------------------------------------------------
    # Time / state sentinels
    # ------------------------------------------------------------------

    TimePassed = "time_passed"
    """Slot is in the past — frozen, no action possible."""

    MissingInputEntities = "missing_input_entities"
    """Critical input data missing — planner cannot run safely."""

    # ------------------------------------------------------------------
    # Charge modes — energy flows INTO the battery
    # ------------------------------------------------------------------

    BatteriesChargeGrid = "batteries_charge_grid"
    """Charge battery from grid import.

    Battery:   charge (up to max_charge_per_slot)
    House:     covered by grid (or PV if available)
    Grid:      import to cover house + charge
    """

    BatteriesChargeSolar = "batteries_charge_solar"
    """Charge battery from PV surplus only — no grid import for charging.

    Battery:   charge from excess PV (after house load is served)
    House:     covered by PV first, grid for remainder
    Grid:      no import for battery; export any PV surplus beyond battery capacity
    """

    EVSmartCharging = "ev_smart_charging"
    """EV is charging — battery must NOT discharge to avoid DC→AC→DC losses.

    Battery:   hold (may charge from PV surplus)
    House:     covered by grid
    Grid:      import for house + EV; export PV surplus
    """

    # ------------------------------------------------------------------
    # Discharge modes — energy flows OUT OF the battery
    # ------------------------------------------------------------------

    BatteriesDischargeMode = "batteries_discharge_mode"
    """Discharge battery to cover house load — no forced export.

    Battery:   discharge (up to max_discharge_per_slot)
    House:     covered by battery first, grid for remainder
    Grid:      import only if battery cannot fully cover house load;
               export only incidental PV surplus
    """

    ForceBatteriesDischarge = "force_batteries_discharge"
    """Force battery discharge — cover house load AND export excess to grid.

    Battery:   discharge at max rate (up to max_discharge_per_slot)
    House:     covered by battery first
    Grid:      EXPORT any battery energy beyond house load;
               import only if battery cannot fully cover house
    """

    ForceExport = "force_export"
    """Force battery discharge directly to grid — house imports separately.

    Battery:   discharge at max rate → ALL energy to grid
    House:     imported from grid (battery does NOT cover house)
    Grid:      EXPORT battery energy; import for house load
    """

    # ------------------------------------------------------------------
    # Passive / idle modes
    # ------------------------------------------------------------------

    BatteriesWaitMode = "batteries_wait_mode"
    """Hold battery charge — neither charge nor discharge.

    Battery:   hold (preserve stored energy for future slots)
    House:     imported from grid
    Grid:      import for house; export any PV surplus
    """


# ---------------------------------------------------------------------------
# Canonical frozensets — import these, never redefine locally
# ---------------------------------------------------------------------------

DISCHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    }
)
"""All modes where the battery discharges energy."""

CHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
        Recommendations.EVSmartCharging.value,
    }
)
"""All modes where the battery charges (or EV charging suppresses discharge)."""
