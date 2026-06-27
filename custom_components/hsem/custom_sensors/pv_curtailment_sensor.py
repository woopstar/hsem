"""Sensor that detects when PV production is being curtailed by the inverter.

Curtailment occurs when the inverter throttles solar production because there
is no place for the energy to go — the battery is full, house consumption is
low, and grid export is blocked (export price below the configured minimum).

The sensor uses two complementary detection methods:

1. **Direct**: Reads ``live.huawei_inverter_active_power_control``.
   If the inverter reports a limit (e.g. ``"Limited to 80%"`` or
   ``"Limited to 100W"``) instead of ``"Unlimited"``, PV is being curtailed.

2. **Derived**: When the battery SoC is high (≥ 95 %) AND the export price
   is below the minimum threshold, curtailment is likely even if the direct
   register has not yet updated.

The sensor state is either ``"curtailed"`` or ``"normal"``.
"""

from __future__ import annotations

from typing import Any, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    CoordinatorData,
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_pv_curtailment_sensor_entity_id,
    get_pv_curtailment_sensor_name,
    get_pv_curtailment_sensor_unique_id,
)

# Recognised "unlimited" (no curtailment) strings from huawei_solar across
# supported locales.  Any value NOT in this set is treated as a limit.
_UNLIMITED_STATES: frozenset[str] = frozenset(
    {
        "unlimited",
        "ikke begrænset",
        "onbeperkt",
        "unbegrenzt",
        "illimitato",
        "sin límite",
        "không giới hạn",
    }
)

# Battery SoC threshold (%) above which curtailment becomes likely when
# combined with export price blocking.
_DERIVED_SOC_THRESHOLD: float = 95.0

# Export price threshold (currency/kWh) below which export is considered
# blocked for the derived detection method.
_DERIVED_EXPORT_PRICE_THRESHOLD: float = 0.01


class HSEMPVTailedSensor(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Sensor detecting PV curtailment from the inverter.

    State is ``"curtailed"`` when PV production is being throttled,
    ``"normal"`` otherwise.
    """

    _attr_icon = "mdi:solar-power"
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the PV curtailment sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared :class:`HSEMDataUpdateCoordinator`.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)

        self._config_entry = config_entry

        self._attr_unique_id = get_pv_curtailment_sensor_unique_id(
            config_entry.entry_id
        )
        self.entity_id = get_pv_curtailment_sensor_entity_id()
        self._name = get_pv_curtailment_sensor_name()

        # Restored state used before the first coordinator cycle completes.
        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def name(self) -> str:
        """Return the display name."""
        return self._name

    @property
    @override
    def unique_id(self) -> str | None:
        """Return the unique ID."""
        return self._attr_unique_id

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        ) or self._restored_state is not None

    @property  # type: ignore[misc]  # HA stub declares state as @final
    @override
    def state(self) -> str:
        """Return ``"curtailed"`` or ``"normal"``."""
        data: CoordinatorData | None = self.coordinator.data
        if data is None or data.live is None:
            return self._restored_state or "normal"

        live = data.live

        # --- Method 1: Direct active power control reading ---
        if _is_directly_limited(live.huawei_inverter_active_power_control):
            return "curtailed"

        # --- Method 2: Derived detection ---
        if _is_derived_curtailment(live):
            return "curtailed"

        return "normal"

    # ------------------------------------------------------------------
    # HA lifecycle
    # ------------------------------------------------------------------

    @override
    async def async_added_to_hass(self) -> None:
        """Restore previous state and register coordinator listener."""
        await super().async_added_to_hass()
        restored = await self.async_get_last_state()
        if restored is not None and restored.state in {"curtailed", "normal"}:
            self._restored_state = restored.state


# ------------------------------------------------------------------
# Detection helpers
# ------------------------------------------------------------------


def _is_directly_limited(power_control_state: str | None) -> bool:
    """Return True if the active power control state indicates a limit.

    Args:
        power_control_state: Raw string from the inverter entity
            (e.g. ``"Unlimited"``, ``"Limited to 80%"``).

    Returns:
        ``True`` if the inverter is actively limiting output.
    """
    if not isinstance(power_control_state, str):
        return False
    return power_control_state.strip().lower() not in _UNLIMITED_STATES


def _is_derived_curtailment(live: Any) -> bool:
    """Return True if derived heuristics indicate curtailment.

    Curtailment is likely when:
    - PV is actually producing (> 0 W)
    - Battery SoC is high (≥ ``_DERIVED_SOC_THRESHOLD``)
    - Export price is below the minimum (effectively blocked)
    - The active power control register is unavailable (None)

    If the register is available and says "Unlimited", the inverter is
    not throttling — the derived heuristics are a fallback only.

    Args:
        live: The current :class:`~models.live_state.LiveState` snapshot.

    Returns:
        ``True`` if derived heuristics suggest curtailment.
    """
    # PV must be producing something.
    if live.solar_production_power_w <= 0:
        return False

    # Battery must be near full.
    soc = live.huawei_batteries_soc_pct
    if soc is None or soc < _DERIVED_SOC_THRESHOLD:
        return False

    # Export must be price-blocked.
    export_price = live.export_electricity_price
    if export_price >= _DERIVED_EXPORT_PRICE_THRESHOLD:
        return False

    # If the active power control register is known and says "Unlimited",
    # the inverter is not throttling — trust the direct reading.
    if live.huawei_inverter_active_power_control is not None:
        return False

    # Register unavailable, PV producing, battery full, export blocked
    # → curtailment is the most likely explanation.
    return True
