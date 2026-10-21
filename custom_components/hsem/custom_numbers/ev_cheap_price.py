"""Number entity for the HSEM EV cheap-price charge threshold.

Defines :class:`HSEMEVCheapPriceNumber`, the per-EV import price at or below
which HSEM charges the EV immediately at full power (issue #1324). It is the
EV option number of :mod:`custom_numbers.ev_target_soc` with a price range
instead of a percentage.

The threshold is compared with the value of the configured import price
sensor, so it has no unit of its own: it is in whatever unit that sensor
reports (for example DKK/kWh).
"""

from __future__ import annotations

from homeassistant.components.number import NumberMode

from custom_components.hsem.const import EV_CHEAP_PRICE_MAX, EV_CHEAP_PRICE_MIN
from custom_components.hsem.custom_numbers.ev_target_soc import HSEMEVTargetSocNumber


class HSEMEVCheapPriceNumber(HSEMEVTargetSocNumber):
    """Per-EV import price threshold for the cheap-price charge.

    Persists to ``config_entry.options`` exactly like the target-SoC number;
    only the range, step, unit and input mode differ.
    """

    _attr_native_min_value = EV_CHEAP_PRICE_MIN
    _attr_native_max_value = EV_CHEAP_PRICE_MAX
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = None
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:cash-clock"
