"""Price sanitisation helpers for the MILP.

Centralises the pre-solve price transformations that keep the LP bounded
and consistent with the physical system:

- NaN handling
- Finite-value normalization
- Battery-export floor mask (issues #752 and #767)

Grid flows are now finitely bounded and direction-exclusive in the MILP, so
finite signed market prices can remain authoritative without unbounded wash
flows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.hsem.utils.logger import log_planner

if TYPE_CHECKING:
    import numpy as np


def sanitize_prices(
    p_imp: np.ndarray,  # type: ignore[name-defined]
    p_exp: np.ndarray,  # type: ignore[name-defined]
    min_export_price: float,
    battery_export_min_price: float,
) -> tuple[
    np.ndarray,  # type: ignore[name-defined]
    np.ndarray,  # type: ignore[name-defined]
    np.ndarray,  # type: ignore[name-defined]
]:
    """Return sanitized import/export arrays and the battery-export block mask.

    The battery-export floor is the maximum of ``min_export_price`` (which
    already includes the depreciation-based ``recommended_threshold`` from
    the engine) and ``battery_export_min_price``.  Slots whose raw export
    price is strictly below the combined floor get ``ed[t]`` capped to
    ``base_load[t] / discharge_eff`` so the battery can serve house load
    but cannot intentionally export to the grid.  PV export is intentionally
    left unrestricted (issue #767).

    Finite signed import/export prices are preserved. Bounded grid variables
    and the binary grid-flow direction constraint prevent unbounded same-slot
    import/export wash flows.

    Args:
        p_imp: Raw import prices per active slot.
        p_exp: Raw export prices per active slot.
        min_export_price: User-configured battery-export floor (combined
            with the engine's recommended threshold by the caller).
        battery_export_min_price: Dedicated per-slot battery-export floor.

    Returns:
        ``(p_imp_obj, p_exp_sanitized, battery_export_blocked)`` where
        ``p_imp_obj`` is the finite signed import price used in the objective,
        ``p_exp_sanitized`` is the finite signed export price, and
        ``battery_export_blocked`` is a bool
        mask of slots below the combined battery-export floor.
    """
    import numpy as np

    # Replace NaN prices with 0 to prevent solver numerical issues.
    p_imp = np.nan_to_num(p_imp, nan=0.0)
    p_exp = np.nan_to_num(p_exp, nan=0.0)

    # Combined battery-export floor (issues #752, #767).
    effective_floor = max(min_export_price, battery_export_min_price)
    battery_export_blocked = np.zeros(len(p_exp), dtype=bool)
    if effective_floor > 1e-9:
        battery_export_blocked = p_exp < effective_floor
        n_blocked = int(np.sum(battery_export_blocked))
        if n_blocked > 0:
            log_planner(
                "debug",
                "[milp] Blocking battery export on %d slots below combined floor "
                "(%.4f) (max blocked=%.4f)",
                n_blocked,
                effective_floor,
                float(np.max(p_exp[battery_export_blocked])),
            )

    p_imp_obj = p_imp.copy()

    return p_imp_obj, p_exp, battery_export_blocked
