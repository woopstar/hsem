"""Resolve active co-optimised EVs and rebuild net load around them.

Extracted from ``milp_optimizer.py`` so it stays under the 30 KB file limit.
Pure move: no behaviour change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import numpy as np

from custom_components.hsem.planner.ev_load_accounting import split_house_and_ev_load
from custom_components.hsem.planner.milp._ev_amp_lattice import ev_has_live_session
from custom_components.hsem.utils.logger import log_planner

if TYPE_CHECKING:
    from custom_components.hsem.models.ev_config import EVConfig
    from custom_components.hsem.models.planned_slot import PlannedSlot


@dataclass(frozen=True)
class ActiveEvNetLoad:
    """Active co-optimised EVs plus the net-load arrays rebuilt around them."""

    active_evs: list[EVConfig]
    net_load: np.ndarray  # type: ignore[name-defined]
    pv_avail: np.ndarray  # type: ignore[name-defined]
    base_load: np.ndarray  # type: ignore[name-defined]


def resolve_active_evs_and_net_load(
    *,
    ev_configs: list[EVConfig] | None,
    slots: list[PlannedSlot],
    future_idx: list[int],
    now: datetime,
    net_load: np.ndarray,  # type: ignore[name-defined]
    pv_avail: np.ndarray,  # type: ignore[name-defined]
    base_load: np.ndarray,  # type: ignore[name-defined]
) -> ActiveEvNetLoad:
    """Resolve which EVs are MILP-co-optimised and rebuild net load if so.

    When ``ev_configs`` is provided, the MILP decides EV charging alongside
    the battery.  Recompute net_load/pv_avail/base_load WITHOUT the
    pre-computed EV planned loads (the LP will decide allocation). Otherwise
    keep the pre-existing EV adjustment (backward-compatible) and return the
    input arrays unchanged.
    """
    active_evs: list[EVConfig] = []
    if ev_configs:
        for ev in ev_configs:
            # A managed live session (managed_session_cap_only, issue #797)
            # has max_charge_per_slot=0 by construction — it must still be
            # admitted so its Huawei discharge permission/ceiling is
            # honoured even though it can command no further charge.
            if (
                ev.enabled
                and ev.capacity_kwh > 1e-9
                and (ev.max_charge_per_slot > 1e-9 or ev_has_live_session(ev))
            ):
                active_evs.append(ev)
        if active_evs:
            # Recompute the pure-house baseline before adding the MILP's EV
            # variables.  Accounted heuristic EV load is already embedded in
            # the house forecast and must be removed, except for a known live
            # session that current-slot injection already subtracted.
            active_net_load: list[float] = []
            for slot_i in future_idx:
                slot = slots[slot_i]
                # Pre-MILP slot population has already classified each EV
                # contribution per baseline provenance, including current-slot
                # live removal. Subtract exactly the aggregate that remains
                # accounted; applying EVConfig flags again would erase a second,
                # still-embedded EV in mixed two-charger cases.
                accounted_to_remove = max(slot.ev_accounted_load_kwh, 0.0)
                house_load, _ev_load = split_house_and_ev_load(
                    slot,
                    accounted_load_kwh=accounted_to_remove,
                )
                active_net_load.append(house_load - slot.solcast_pv_estimate_kwh)
            net_load = np.asarray(active_net_load, dtype=float)
            pv_avail = np.maximum(-net_load, 0.0)
            base_load = np.maximum(net_load, 0.0)
            log_planner(
                "debug",
                "[milp] EV co-optimisation enabled: %d active EV(s), "
                "net_load rebuilt without double-counted EV loads",
                len(active_evs),
            )
        else:
            active_evs = []
    if not active_evs and ev_configs:
        log_planner(
            "debug",
            "[milp] EV configs provided but no valid active EVs — "
            "falling back to fixed EV loads",
        )

    return ActiveEvNetLoad(
        active_evs=active_evs,
        net_load=net_load,
        pv_avail=pv_avail,
        base_load=base_load,
    )
