"""Dataclass for one EV configuration in the MILP optimizer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EVConfig:
    """Configuration for one EV in the MILP optimizer.

    The MILP treats each EV as a flexible load with a deadline target.
    It co-optimises EV charging alongside battery charge/discharge and grid
    import/export, allocating PV surplus and cheap grid slots across all
    consumers simultaneously.

    Attributes:
        enabled: ``True`` when this EV should be optimised by the MILP.
        initial_soc_kwh: EV battery energy at the start of the planning
            horizon (kWh, ≥ 0).  This is SoC% × capacity / 100.
        target_kwh: Desired EV battery energy by the deadline (kWh).
        capacity_kwh: EV battery nameplate capacity in kWh.
        max_charge_per_slot: Maximum DC-side energy deliverable per slot
            (kWh).  Accounted for charger efficiency: the charger draws
            ``max_charge_per_slot / charger_efficiency`` from AC.
        charger_efficiency: Charger efficiency as a fraction (0.01–1.0).
            ``ev_c[t] / charger_efficiency`` is the AC-side grid/PV draw.
        deadline_slot: Index into the LP's future-slot list (0..m-1) of the
            last slot that can be used to meet the target.  Slots beyond this
            index may still charge but the target must be met by this slot.
            ``None`` means no deadline (skip the deadline soft constraint).
        base_load_includes_ev: When ``True``, EV charging power is already
            captured in the house consumption sensor.  The MILP will mark
            the EV load as accounted rather than planned (affects how the
            results are written to ``PlannedSlot`` fields).
    """

    enabled: bool = False
    initial_soc_kwh: float = 0.0
    target_kwh: float = 0.0
    capacity_kwh: float = 0.0
    max_charge_per_slot: float = 0.0
    charger_efficiency: float = 1.0
    deadline_slot: int | None = None
    base_load_includes_ev: bool = False
