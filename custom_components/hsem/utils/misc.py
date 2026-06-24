"""General-purpose utility functions for the HSEM custom integration.

Includes helpers for config value retrieval, hashing, efficiency
clamping, battery power calculations, and cycle-cost thresholds.
"""

import hashlib
from typing import Any

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.utils.conversion import convert_months_to_int  # noqa: F401


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

    Supports both old (S0: 5/10/15 kWh) and new (S1: 7/14/21 kWh) series.

    Args:
        usable_capacity: The usable battery capacity in watt-hours.

    Returns:
        The maximum discharge power in watts.  Defaults to 2500 W for
        unknown capacities.
    """
    mapping = {
        # Old batteries (S0)
        5000: 2500,
        10000: 5000,
        15000: 5000,
        # New batteries (S1)
        7000: 3500,
        14000: 7000,
        21000: 10500,
    }
    return mapping.get(usable_capacity, 2500)


def calculate_recommended_threshold(
    purchase_price: float,
    expected_cycles: int,
    usable_capacity: float,
    capacity_loss_pct: float = 30.0,
) -> float:
    """Calculate the recommended price threshold based on battery depreciation.

    The threshold represents the minimum price spread required for grid
    charging to be economically rational.  It covers only battery
    depreciation — conversion (in)efficiency losses are handled separately
    by the MILP objective and the cost function's ``conversion_loss_cost``
    term, both of which price the losses using the actual import price of
    each slot rather than a fixed add-on.

    **Depreciation term**::

        depreciation = (purchase_price × capacity_loss_pct / 100)
                       / (2 × usable_capacity × expected_cycles)

    The ``2×`` factor accounts for one full cycle (charge + discharge).

    Args:
        purchase_price: Total battery system cost in local currency.
        expected_cycles: Total expected lifetime charge/discharge cycles.
        usable_capacity: Usable battery capacity in kWh.
        capacity_loss_pct: Battery capacity lost at end-of-life as a percentage
            of original capacity (0-100).  LiFePO4 EOL is typically defined at
            80% retained capacity = 20% loss.  Defaults to 30% to account for
            both EOL degradation and calendar ageing.

    Returns:
        Depreciation cost per kWh of battery throughput, rounded to 3 decimal
        places (local currency / kWh).
    """
    if purchase_price <= 0 or expected_cycles <= 0 or usable_capacity <= 0:
        return 0.0

    # Depreciation cost per kWh of throughput.
    # The 2× factor accounts for charge + discharge per full cycle.
    # Capacity loss accounts for residual value at end-of-life.
    capacity_loss_dec = max(min(capacity_loss_pct, 100.0), 0.0) / 100.0
    depr = (purchase_price * capacity_loss_dec) / (
        2 * expected_cycles * usable_capacity
    )

    return round(depr, 3)
