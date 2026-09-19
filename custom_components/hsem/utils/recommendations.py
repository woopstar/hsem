"""Recommendations enumeration for HSEM planner.

Defines the canonical set of battery/house/grid operating modes with
clear semantics for the SoC simulation and hardware applier.

Usage
-----
>>> from custom_components.hsem.utils.recommendations import Recommendations
>>> slot.recommendation = Recommendations.BatteriesDischargeMode.value
"""

from dataclasses import dataclass
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
    """EV is charging — a display relabel applied after SoC simulation.

    The underlying energy flows (charge/discharge/import/export) are
    computed by the SoC simulation *before* this label is applied and are
    unaffected by it: the battery remains free to discharge for non-EV
    house load (issue #862). Only the EV's own load is guaranteed to be
    served from grid/PV, never the battery.

    Battery:   whatever the simulation already solved (may discharge for
               house load, charge from PV surplus, or hold)
    House:     covered by battery first (if solved), grid for remainder
    Grid:      import for EV + any uncovered house load; export PV surplus
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

    BatteriesDischargeWindowMode = "batteries_discharge_window_mode"
    """Inside a seasonal discharge window, but the planner holds the battery.

    Assigned by the seasonal fill in ``apply_optimization_strategy`` to a
    summer slot with no PV surplus, yet the solved plan schedules no battery
    discharge this interval (prices are not high enough or the energy is
    reserved for a later slot).  Fixed user-configured schedule windows were
    removed in issue #860, so this label has no configured-window producer.
    Self-consumption discharge is still allowed: the inverter runs in
    MaximizeSelfConsumption and the firmware may ramp discharge up to the
    ceiling to cover live house load.

    Battery:   hold by plan; may self-consume up to max_discharge_per_slot
    House:     covered by battery first, grid for remainder
    Grid:      import only if battery cannot fully cover house load;
               export only incidental PV surplus
    """

    ForceBatteriesDischarge = "force_batteries_discharge"
    """Force battery discharge — cover house AND export excess to grid.

    Battery:   discharge at max rate (up to max_discharge_per_slot)
    House:     covered by battery first (AC bus)
    Grid:      EXPORT any battery energy beyond house load;
               import only if battery cannot fully cover house

    Per Huawei wiki: \"forces the inverter to inject more power to the
    AC-side than your home electricity usage, which will consequently
    be pushed onto the grid.\"
    """

    ForceExport = "force_export"
    """Force PV export to grid — sets inverter to FullyFedToGrid mode.

    Battery:   unchanged (may still charge/discharge per schedule)
    House:     covered by grid import (PV bypasses house)
    Grid:      ALL PV production exported to grid

    Different from ForceBatteriesDischarge: this mode changes the
    INVERTER behavior (PV routing), not the battery.  The battery
    continues normal operation.
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
        Recommendations.BatteriesDischargeWindowMode.value,
        Recommendations.ForceBatteriesDischarge.value,
        Recommendations.ForceExport.value,
    }
)
"""All modes where the battery discharges energy.

Includes ``batteries_discharge_window_mode`` so that discharge-window
logic (concentration, window hysteresis, replacement-price derivation)
treats the held-window label as part of the discharge schedule.
"""

CHARGE_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
        Recommendations.EVSmartCharging.value,
    }
)
"""All modes where the battery charges (or EV charging suppresses discharge)."""

SENTINEL_RECS: frozenset[Recommendations] = frozenset(
    {
        Recommendations.TimePassed,
        Recommendations.MissingInputEntities,
    }
)
"""Inert planner-state sentinels that never represent a hardware action."""

USER_SELECTABLE_RECS: tuple[str, ...] = tuple(
    sorted(member.value for member in Recommendations if member not in SENTINEL_RECS)
)
"""Every mode a user may force, in stable alphabetical order.

Excludes only the two state sentinels (``time_passed``,
``missing_input_entities``), which describe planner state rather than an
operating mode and cannot be commanded.

Import this everywhere a user-facing override surface enumerates modes — the
``select`` platform's force-working-mode entity, the
``set_temporary_override`` service schema, and ``services.yaml``.  These drifted
apart once already (``batteries_discharge_window_mode`` reached ``services.yaml``
and ``select.py`` but not the service validator, so the UI offered a mode the
schema then rejected), which is why the list is derived from the enum rather
than restated per surface.
"""

BATTERY_CHARGE_ACTION_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesChargeGrid.value,
        Recommendations.BatteriesChargeSolar.value,
    }
)
"""Modes where the *label itself* commits the primary battery to charging.

Deliberately narrower than :data:`CHARGE_RECS`: ``ev_smart_charging`` is a
display relabel applied *after* the SoC simulation has already solved the
battery's own flows, so it says nothing about the battery's direction (the
battery may charge, discharge for house load, or hold).  Use
:data:`CHARGE_RECS` to ask "does this slot involve charging at all"; use this
set to ask "did the plan commit the battery to charge".
"""

BATTERY_DISCHARGE_ACTION_RECS: frozenset[str] = frozenset(
    {
        Recommendations.BatteriesDischargeMode.value,
        Recommendations.ForceBatteriesDischarge.value,
    }
)
"""Modes where the *label itself* commits the primary battery to discharging.

Deliberately narrower than :data:`DISCHARGE_RECS`, which exists for
discharge-*window* logic and so also covers labels that do not dispatch the
battery:

- ``batteries_discharge_window_mode`` — the plan holds the battery this slot
  (self-consumption is permitted, but nothing is dispatched).
- ``force_export`` — changes inverter PV routing (``FullyFedToGrid``); the
  battery is left unchanged and may still charge, discharge, or hold.

Use :data:`DISCHARGE_RECS` for window/schedule logic; use this set to ask
"did the plan commit the battery to discharge".
"""


# ---------------------------------------------------------------------------
# Label ⇒ energy contract (issue #1035)
# ---------------------------------------------------------------------------

MATERIAL_ENERGY_KWH: float = 1e-9
"""Epsilon above which a slot's battery energy counts as *material*.

Matches the threshold every existing label/energy guard already uses
(``soc_simulation.py``, ``concentrate_discharge_on_expensive_slots``), so the
self-consistency check and the guards that enforce it agree exactly.
"""


class EnergyExpectation(Enum):
    """What a recommendation label promises about one battery energy field."""

    MATERIAL = "material"
    """The field must exceed :data:`MATERIAL_ENERGY_KWH`."""

    ZERO = "zero"
    """The field must not exceed :data:`MATERIAL_ENERGY_KWH`."""

    UNCONSTRAINED = "unconstrained"
    """The label makes no promise about this field — explicitly undecidable."""


@dataclass(frozen=True)
class LabelEnergyContract:
    """The energy a recommendation label promises about its own slot.

    Attributes:
        charge: Expectation for ``PlannedSlot.batteries_charged_kwh``.
        discharge: Expectation for ``PlannedSlot.batteries_discharged_kwh``.
        rationale: Why the contract is what it is — in particular why a field
            is ``UNCONSTRAINED``, so "no guarantee" reads as a decision rather
            than an oversight.
    """

    charge: EnergyExpectation
    discharge: EnergyExpectation
    rationale: str


_MATERIAL = EnergyExpectation.MATERIAL
_ZERO = EnergyExpectation.ZERO
_UNCONSTRAINED = EnergyExpectation.UNCONSTRAINED

LABEL_ENERGY_CONTRACTS: dict[Recommendations, LabelEnergyContract] = {
    Recommendations.TimePassed: LabelEnergyContract(
        charge=_UNCONSTRAINED,
        discharge=_UNCONSTRAINED,
        rationale=(
            "Sentinel, not an operating mode. A past slot is a frozen record of "
            "what already happened, so both fields are history rather than a "
            "promise."
        ),
    ),
    Recommendations.MissingInputEntities: LabelEnergyContract(
        charge=_UNCONSTRAINED,
        discharge=_UNCONSTRAINED,
        rationale=(
            "Sentinel, not an operating mode. The planner could not run, so no "
            "energy claim was made."
        ),
    ),
    Recommendations.BatteriesChargeGrid: LabelEnergyContract(
        charge=_MATERIAL,
        discharge=_ZERO,
        rationale=(
            "The applier opens a TOU grid-charge window for this label, so the "
            "battery physically absorbs energy. A zero-charge slot is issue "
            "#989's plan-vs-actuator contradiction."
        ),
    ),
    Recommendations.BatteriesChargeSolar: LabelEnergyContract(
        charge=_MATERIAL,
        discharge=_ZERO,
        rationale=(
            "The applier drives MaximizeSelfConsumption, which absorbs live PV, "
            "so a zero-charge slot destroys reserved headroom (issue #989)."
        ),
    ),
    Recommendations.EVSmartCharging: LabelEnergyContract(
        charge=_UNCONSTRAINED,
        discharge=_UNCONSTRAINED,
        rationale=(
            "DECIDED: no battery guarantee. This is a display relabel applied "
            "by ``_label_commanded_ev_slots`` *after* the SoC simulation has "
            "already solved the battery's flows, and it overwrites whatever "
            "label the slot had. It states that HSEM commands a charger, not "
            "what the battery does: the battery may charge from PV, discharge "
            "for non-EV house load (issue #862), or hold. Only the EV's own "
            "load is guaranteed never to be served from the battery, and that "
            "is enforced at the hardware layer, not by this label. Membership "
            "in CHARGE_RECS is for 'does this slot involve charging at all' — "
            "see BATTERY_CHARGE_ACTION_RECS for the narrower question."
        ),
    ),
    Recommendations.BatteriesDischargeMode: LabelEnergyContract(
        charge=_ZERO,
        discharge=_MATERIAL,
        rationale=(
            "An active discharge label that dispatches nothing publishes a "
            "discharge the plan never made (issue #1026)."
        ),
    ),
    Recommendations.ForceBatteriesDischarge: LabelEnergyContract(
        charge=_ZERO,
        discharge=_MATERIAL,
        rationale=(
            "Forces the inverter to inject beyond house load. ``simulate_soc`` "
            "clears the label to wait mode when the simulated discharge is "
            "zero, so a published slot always dispatches."
        ),
    ),
    Recommendations.BatteriesDischargeWindowMode: LabelEnergyContract(
        charge=_ZERO,
        discharge=_ZERO,
        rationale=(
            "The plan holds the battery this interval — it is the demotion "
            "target for a discharge label that dispatched nothing (issue "
            "#1026). Firmware self-consumption may still run, but the *plan* "
            "schedules no battery energy either way."
        ),
    ),
    Recommendations.ForceExport: LabelEnergyContract(
        charge=_ZERO,
        discharge=_MATERIAL,
        rationale=(
            "DECIDED: material discharge, no charge. The enum docstring's "
            "'battery unchanged' describes the *applier* (FullyFedToGrid "
            "re-routes PV, it does not command the battery), but the planner's "
            "own simulation treats the label as a max-rate discharge and "
            "``soc_simulation.py`` clears it to wait mode whenever that "
            "resolves to zero. Every published force_export slot therefore "
            "carries material discharge, and nothing in the planner ever "
            "schedules charge into one. The permissive reading was never what "
            "the code did; pinning it down here is the point of issue #1035."
        ),
    ),
    Recommendations.BatteriesWaitMode: LabelEnergyContract(
        charge=_ZERO,
        discharge=_ZERO,
        rationale=(
            "Strict Wait executes as 0 W at the inverter, so any energy on a "
            "wait slot contradicts the command being sent (issue #1032)."
        ),
    ),
}
"""The single source of truth for what each label promises about its slot.

Every :class:`Recommendations` member must appear here — a member without an
entry fails ``tests/planner/test_plan_consistency.py``, which is what forces a
new label to come with an explicit decision instead of a silent omission.

Checked against the *selected* plan by
:func:`custom_components.hsem.planner.plan_consistency.check_plan_self_consistency`.
See ``docs/planner-spec.md`` § "Label/energy self-consistency".
"""
