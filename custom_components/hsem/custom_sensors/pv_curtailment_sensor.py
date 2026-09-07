"""Sensor that detects when PV production is being curtailed by the inverter.

Curtailment occurs when the inverter throttles solar production because there
is no place for the energy to go — the battery is full, house consumption is
low, and grid export is physically blocked. Since issue #767, HSEM only
physically blocks the whole grid connection point when the export price is
**negative** (exporting would cost money); a positive-but-low export price
below ``export_electricity_min_price``/``batteries_export_min_price`` only
gates intentional *battery*-to-grid export, PV surplus keeps exporting
normally. Do not conflate "export price below the configured minimum" with
"grid export is blocked" — see ``applier_power_control.py``.

The sensor uses two complementary detection methods:

1. **Direct**: Reads ``live.huawei_inverter_active_power_control``.
   If the inverter reports a limit (e.g. ``"Limited to 80%"`` or
   ``"Limited to 100W"``) that sits *below* the routine, always-applied
   ``max_grid_export_power_kw`` DNO/grid cap (or below ``"Unlimited"`` when no
   cap is configured), PV is being actively curtailed. A limit that merely
   matches the configured cap is normal steady-state operation, not
   curtailment (issue #924) — the applier writes that cap for every
   non-negative export price regardless of price-based curtailment.

2. **Derived**: When the battery SoC is high (≥ 95 %) AND the export price
   is negative (the only case where HSEM itself blocks grid export),
   curtailment is likely even if the direct register has not yet updated.

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
from custom_components.hsem.custom_sensors.applier_state_readers import (
    _is_watt_limit,
    _parse_power_control_pct,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_pv_curtailment_sensor_entity_id,
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

# Export price (currency/kWh) at/above which the applier keeps the grid
# connection point unlimited (issue #767) — PV surplus export is never
# blocked by ``export_electricity_min_price``/``batteries_export_min_price``,
# only intentional battery-to-grid export is gated. Only a strictly negative
# export price causes the applier to physically block the whole connection
# point, so that is the only price condition the derived heuristic may use.
_DERIVED_EXPORT_PRICE_THRESHOLD: float = 0.0

# Tolerance (W) when comparing the inverter's reported watt limit against the
# expected routine grid-export cap, to absorb minor Modbus read-back rounding.
_CURTAILMENT_TOLERANCE_WATT: float = 5.0


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
    _attr_translation_key = "pv_curtailment"
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

        # Restored state used before the first coordinator cycle completes.
        self._restored_state: str | None = None

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

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
        if _is_directly_limited(live.huawei_inverter_active_power_control, data.cfg):
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


def _is_directly_limited(
    power_control_state: str | None, cfg: SensorConfig | None
) -> bool:
    """Return True if the active power control state indicates real curtailment.

    A reported limit is only genuine curtailment when it sits *below* the
    routine, always-applied baseline for the current configuration:

    - No ``max_grid_export_power_kw`` cap configured → baseline is
      ``"Unlimited"``; any reported limit is curtailment.
    - A cap is configured → the applier writes that cap in watts for every
      non-negative export price (issue #767), so the register normally reads
      a matching watt limit even with no price-based curtailment in effect.
      Only a watt limit *below* the configured cap indicates the applier
      actively curtailed further (e.g. the negative-price export block).

    Args:
        power_control_state: Raw string from the inverter entity
            (e.g. ``"Unlimited"``, ``"Limited to 80%"``, ``"Limited to 100W"``).
        cfg: Current sensor configuration, used to resolve the routine
            grid-export cap baseline. ``None`` falls back to the legacy
            any-limit-is-curtailment behaviour.

    Returns:
        ``True`` if the inverter is actively curtailing output beyond the
        routine configured cap.
    """
    if not isinstance(power_control_state, str):
        return False
    normalized = power_control_state.strip().lower()
    if normalized in _UNLIMITED_STATES:
        return False

    cap_kw = cfg.max_grid_export_power_kw if cfg is not None else 0.0
    if cap_kw <= 1e-9:
        # No routine cap configured — any reported limit is curtailment.
        return True

    if not _is_watt_limit(power_control_state):
        # A percentage-based limit while a watt-based cap is configured is
        # not the applier's routine steady state.
        return True

    parsed_watts = _parse_power_control_pct(power_control_state)
    if parsed_watts is None:
        # Unparseable limit string — cannot confirm it matches the routine
        # cap, so report it for visibility rather than hide a real problem.
        return True

    baseline_watts = cap_kw * 1000.0
    return parsed_watts < baseline_watts - _CURTAILMENT_TOLERANCE_WATT


def _is_derived_curtailment(live: Any) -> bool:
    """Return True if derived heuristics indicate curtailment.

    Curtailment is likely when:
    - PV is actually producing (> 0 W)
    - Battery SoC is high (≥ ``_DERIVED_SOC_THRESHOLD``)
    - Export price is negative (the only case where HSEM's applier
      physically blocks the whole grid connection point — issue #767)
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

    # Export must be negative-price-blocked (issue #767 — a positive-but-low
    # price below the configured minimum does NOT block PV export, only
    # intentional battery-to-grid export).
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
