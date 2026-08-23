"""General-purpose utility functions for the HSEM custom integration.

Includes helpers for config value retrieval, hashing, efficiency
clamping, battery power calculations, and cycle-cost thresholds.
"""

import hashlib
from typing import Any

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.utils.conversion import convert_months_to_int  # noqa: F401
from custom_components.hsem.utils.logger import log_planner


def generate_hash(input_sensor: str) -> str:
    """Generate an SHA-256 hash based on the input sensor's name."""
    return hashlib.sha256(input_sensor.encode("utf-8")).hexdigest()


def get_config_value(config_entry: Any | None, key: str) -> Any:
    """Get a configuration value from options, data, or defaults.

    Looks up the key in the config entry's ``options`` first, then
    ``data``, and finally falls back to ``DEFAULT_CONFIG_VALUES``.

    Args:
        config_entry: The Home Assistant config entry, or None.
        key: The configuration key to look up.

    Returns:
        The resolved configuration value.

    Raises:
        KeyError: If the key is not present in DEFAULT_CONFIG_VALUES.
    """
    if key not in DEFAULT_CONFIG_VALUES:
        raise KeyError(f"Key '{key}' not found in DEFAULT_VALUES")

    if config_entry is None and key in DEFAULT_CONFIG_VALUES:
        return DEFAULT_CONFIG_VALUES[key]

    if config_entry is None:
        return None

    data = config_entry.options.get(
        key, config_entry.data.get(key, DEFAULT_CONFIG_VALUES[key])
    )

    if data is None:
        return DEFAULT_CONFIG_VALUES[key]

    return data


def ema_filter(
    current: float,
    previous: float | None,
    alpha: float,
) -> float:
    """Apply an exponential moving average filter.

    Smooths a stream of values by blending each new reading with the
    previous smoothed value.  The *alpha* parameter controls
    responsiveness:

    - ``alpha = 1.0`` → no smoothing (raw value).
    - ``alpha = 0.3`` → each new reading contributes 30 %.
    - ``alpha = 0.0`` → frozen (always returns *previous*).

    On the first call (``previous is None``) the raw ``current`` value is
    returned as-is to initialise the filter.

    Args:
        current: The latest raw value.
        previous: The previous EMA-smoothed value, or ``None`` to
            initialise.
        alpha: Smoothing factor in [0.0, 1.0].

    Returns:
        The new EMA-smoothed value.
    """
    if previous is None:
        return current
    return alpha * current + (1.0 - alpha) * previous


def clamp_efficiency(pct: float) -> float:
    """Convert an efficiency percentage (0-100) to a fraction (0.01-1.0).

    Clamps input to [1.0, 100.0] before dividing by 100 so downstream
    code never divides by zero or exceeds 100% efficiency.

    Args:
        pct: Efficiency as a percentage, e.g. 97.0 for 97%.

    Returns:
        Efficiency as a fraction in [0.01, 1.0].
    """
    return max(min(pct, 100.0), 1.0) / 100.0


def get_max_discharge_power(usable_capacity: int) -> int:
    """Return the maximum discharge power in watts for a Huawei battery.

    Supports both old (S0: 5 kWh modules) and new (S1: 7 kWh modules)
    series, including two-stack combinations.

    The S0 entries follow a 2.5 kW-per-module rule with a 5 kW per-stack
    ceiling.  Two-stack totals are the sum of the per-stack limits, so
    30000 Wh (2× 15 kWh) maps to 10 kW.  20000 Wh is ambiguous (15+5 vs
    10+10) and uses the safe 15+5 value of 7500 W.

    Args:
        usable_capacity: The usable battery capacity in watt-hours.

    Returns:
        The maximum discharge power in watts.  Defaults to 2500 W for
        unknown capacities and logs a warning, since a silent fallback
        previously caused under-powered plans for unlisted capacities.
    """
    mapping = {
        # Old batteries (S0) — single stack
        5000: 2500,
        10000: 5000,
        15000: 5000,
        # Old batteries (S0) — two stacks
        20000: 7500,
        25000: 10000,
        30000: 10000,
        # New batteries (S1)
        7000: 3500,
        14000: 7000,
        21000: 10500,
    }
    if usable_capacity not in mapping:
        log_planner(
            "warning",
            "[battery] get_max_discharge_power  capacity=%d Wh not in "
            "known table — falling back to %d W. Check that this matches "
            "the physical discharge capability.",
            usable_capacity,
            2500,
        )
    return mapping.get(usable_capacity, 2500)


def resolve_cycle_cost(
    purchase_price: float,
    usable_kwh: float,
    expected_cycles: int,
    capacity_loss_pct: float = 30.0,
    user_margin: float = 0.0,
) -> float:
    """Canonical battery cycle depreciation cost per kWh of throughput.

    **This is the single source of truth** for cycle cost across the entire
    codebase — MILP objective, cost function, heuristic charge passes, and
    recommended threshold all derive from this one function.

    Formula::

        auto = (purchase_price × capacity_loss_pct / 100)
               / (2 × usable_kwh × expected_cycles)

        result = max(auto, user_margin)

    The ``2×`` factor accounts for one full cycle (charge + discharge).
    ``capacity_loss_pct`` accounts for the fractional capacity lost over the
    battery's lifetime (e.g. 30 % loss at EOL, 70 % retained).

    ``user_margin`` acts as a floor: set a positive value to add extra
    friction beyond the auto-calculated depreciation.  Returns 0.0 when
    any required value is non-positive.

    Args:
        purchase_price: Total battery system cost in local currency.
        usable_kwh: Usable battery capacity in kWh (live, not rated).
        expected_cycles: Total expected lifetime charge/discharge cycles.
        capacity_loss_pct: Battery capacity lost at end-of-life as a
            percentage of original capacity (0-100).  LiFePO4 EOL is
            typically defined at 80 % retained = 20 % loss.
            Default 30 % includes margin for calendar ageing.
        user_margin: Additional per-kWh margin (≥ 0) added to the
            auto-calculated cost.  Default 0.0 (no extra margin).

    Returns:
        Depreciation cost per kWh of battery throughput (local currency / kWh).
    """
    if purchase_price <= 1e-9 or expected_cycles <= 0 or usable_kwh <= 1e-9:
        result = max(0.0, user_margin)
        log_planner(
            "debug",
            "[cycle_cost] resolve_cycle_cost  auto=0.000000  "
            "user_margin=%.6f  result=%.6f  "
            "reason=missing_input (price=%.2f cycles=%d usable=%.3f)",
            user_margin,
            result,
            purchase_price,
            expected_cycles,
            usable_kwh,
        )
        return result

    capacity_loss_dec = max(min(capacity_loss_pct, 100.0), 0.0) / 100.0
    auto = (purchase_price * capacity_loss_dec) / (2 * expected_cycles * usable_kwh)
    result = max(auto, user_margin)
    log_planner(
        "debug",
        "[cycle_cost] resolve_cycle_cost  auto=%.6f  user_margin=%.6f  "
        "result=%.6f  "
        "inputs=(price=%.2f usable=%.3f cycles=%d loss=%.1f%%)",
        auto,
        user_margin,
        result,
        purchase_price,
        usable_kwh,
        expected_cycles,
        capacity_loss_pct,
    )
    return result


def calculate_recommended_threshold(
    purchase_price: float,
    expected_cycles: int,
    usable_capacity: float,
    capacity_loss_pct: float = 30.0,
) -> float:
    """Calculate the recommended price threshold based on battery depreciation.

    Deprecated thin wrapper around :func:`resolve_cycle_cost` that rounds
    the result to 3 decimal places for display purposes.

    The threshold represents the minimum price spread required for grid
    charging to be economically rational.  It covers only battery
    depreciation. Conversion (in)efficiency is already physical in the MILP's
    AC grid draw and delivery and therefore needs no separate price add-on.

    Args:
        purchase_price: Total battery system cost in local currency.
        expected_cycles: Total expected lifetime charge/discharge cycles.
        usable_capacity: Usable battery capacity in kWh.
        capacity_loss_pct: Battery capacity lost at end-of-life as a
            percentage of original capacity (0-100).  Defaults to 30 %.

    Returns:
        Depreciation cost per kWh of battery throughput, rounded to 3 decimal
        places (local currency / kWh).
    """
    return round(
        resolve_cycle_cost(
            purchase_price=purchase_price,
            usable_kwh=usable_capacity,
            expected_cycles=expected_cycles,
            capacity_loss_pct=capacity_loss_pct,
        ),
        3,
    )
