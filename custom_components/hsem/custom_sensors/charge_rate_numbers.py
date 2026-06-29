"""Number entities for temperature-bucketed charge rate learning.

Exposes 7 editable :class:`NumberEntity` instances — one per temperature
bucket — that display the p90 learned charge rate and allow manual override.

The learned values come from the module-level :class:`ChargeRateLearner`
singleton.  User overrides are persisted to the config entry options so
they survive HA restarts.

Issue #608 — Temperature-adaptive battery charge rate learning.
"""

from __future__ import annotations

import json
from typing import override

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant

from custom_components.hsem.entity import HSEMEntity
from custom_components.hsem.utils.charge_rate_learner import (
    CHARGE_RATE_LEARNER,
    TEMP_BUCKETS,
)
from custom_components.hsem.utils.conversion import convert_to_float

# Config entry key for persisting learned charge rates across restarts.
_LEARNED_RATES_KEY = "hsem_charge_rate_learned_rates"


def _override_key(bucket_name: str) -> str:
    """Return the config entry option key for a manual override."""
    return f"hsem_charge_rate_override_{bucket_name}"


class HSEMChargeRateNumber(HSEMEntity, NumberEntity):
    """Number entity for a temperature-bucket charge rate.

    Displays the p90 learned charge rate for the bucket.  When the
    learner has not collected enough samples the entity reports
    ``available = False`` (unavailable).  The user can set a manual
    override; overrides are persisted to config entry options.
    """

    _attr_has_entity_name = True
    _attr_native_min_value = 0.0
    _attr_native_max_value = 25000.0
    _attr_native_step = 100.0
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_icon = "mdi:thermometer-lines"

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        description: NumberEntityDescription,
        bucket_name: str,
        *,
        unique_id: str = "",
        entity_id: str = "",
    ) -> None:
        """Initialize the charge rate number entity.

        Args:
            hass: The Home Assistant instance.
            config_entry: The config entry this entity belongs to.
            description: Entity description carrying ``key`` and ``translation_key``.
            bucket_name: The temperature bucket this entity represents.
            unique_id: Stable unique ID for HA entity registry.
            entity_id: The desired entity_id string for this entity.
        """
        super().__init__(config_entry)

        self.hass = hass
        self._config_entry = config_entry
        self._bucket_name = bucket_name
        self.entity_description = description
        self._attr_unique_id = unique_id if unique_id else description.key
        if entity_id:
            self.entity_id = entity_id

        raw_name = description.name
        if isinstance(raw_name, str):
            self._attr_name = str(raw_name)

        # Load override from config entry options, if any (optional).
        override_key = _override_key(bucket_name)
        stored = None
        if config_entry:
            raw = config_entry.options.get(
                override_key, config_entry.data.get(override_key)
            )
            stored = convert_to_float(raw)
        self._override: float | None = stored

    # ------------------------------------------------------------------
    # State properties — dynamically read from learner or override
    # ------------------------------------------------------------------

    @property
    @override
    def available(self) -> bool:
        """Entity is always available — shows 0 W when no data yet."""
        return True

    @property
    @override
    def native_value(self) -> float | None:
        """Return the current effective charge rate.

        Manual overrides take priority over learned rates.
        Returns 0 when neither exists (not yet learned).
        """
        if self._override is not None:
            return self._override
        learned = CHARGE_RATE_LEARNER.learned_rates.get(self._bucket_name)
        return learned if learned is not None else 0.0

    # ------------------------------------------------------------------
    # Manual override — user sets a value via the UI
    # ------------------------------------------------------------------

    @override
    async def async_set_native_value(self, value: float) -> None:
        """Handle the user setting a manual override.

        Args:
            value: The new override value in watts.
        """
        clamped = max(
            self._attr_native_min_value, min(self._attr_native_max_value, value)
        )
        self._override = clamped

        # Persist to config entry options so it survives restart.
        new_options = {
            **self._config_entry.options,
            _override_key(self._bucket_name): clamped,
        }
        self.hass.config_entries.async_update_entry(
            self._config_entry, options=new_options
        )
        self.async_write_ha_state()

    @override
    async def async_added_to_hass(self) -> None:
        """Restore learned rates from config entry and register listeners."""
        await super().async_added_to_hass()

        # Restore learned rates from config entry so they survive HA restarts.
        _restore_learned_rates_from_entry(self._config_entry)

        self.async_on_remove(
            self._config_entry.add_update_listener(self._async_handle_config_update)
        )

    async def _async_handle_config_update(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        """Re-read the override value from the updated config entry."""
        override_key = _override_key(self._bucket_name)
        stored = None
        raw = entry.options.get(override_key, entry.data.get(override_key))
        if raw is not None:
            stored = convert_to_float(raw)
        self._override = stored
        self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Persistence helpers — restore and persist learned rates across restarts
# ---------------------------------------------------------------------------


def _restore_learned_rates_from_entry(config_entry: ConfigEntry) -> None:
    """Restore learned charge rates from config entry options into the learner.

    Reads the JSON blob stored under ``_LEARNED_RATES_KEY`` and populates
    ``CHARGE_RATE_LEARNER.learned_rates``.  No-op if the key is absent or
    the JSON is malformed.
    """
    raw = config_entry.options.get(
        _LEARNED_RATES_KEY, config_entry.data.get(_LEARNED_RATES_KEY)
    )
    if not raw or not isinstance(raw, str):
        return
    try:
        stored: dict[str, float | None] = json.loads(raw)
    except json.JSONDecodeError, TypeError:
        return
    for name, _lo, _hi in TEMP_BUCKETS:
        rate = stored.get(name)
        if rate is not None:
            CHARGE_RATE_LEARNER.learned_rates[name] = float(rate)


def persist_learned_rates_to_entry(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> None:
    """Persist the current learned charge rates to the config entry.

    Called from the coordinator after the learner produces new rates.
    Only rates that are not ``None`` are stored.
    """
    payload = {
        name: rate
        for name, rate in CHARGE_RATE_LEARNER.learned_rates.items()
        if rate is not None
    }
    if not payload:
        return
    stored_raw = config_entry.options.get(
        _LEARNED_RATES_KEY, config_entry.data.get(_LEARNED_RATES_KEY)
    )
    try:
        existing: dict[str, float] = json.loads(stored_raw) if stored_raw else {}
    except json.JSONDecodeError, TypeError:
        existing = {}
    if existing == payload:
        return  # No change — avoid unnecessary writes.
    new_options = {**config_entry.options, _LEARNED_RATES_KEY: json.dumps(payload)}
    hass.config_entries.async_update_entry(config_entry, options=new_options)


# ---------------------------------------------------------------------------
# Entity creation helper
# ---------------------------------------------------------------------------


def _bucket_order(bucket_name: str) -> int:
    """Return the natural order index for a bucket name."""
    for i, (name, _, _) in enumerate(TEMP_BUCKETS):
        if name == bucket_name:
            return i
    return 99


def create_charge_rate_number_entities(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
) -> list[HSEMChargeRateNumber]:
    """Create one :class:`HSEMChargeRateNumber` per temperature bucket.

    Args:
        hass: The Home Assistant instance.
        config_entry: The config entry this entity belongs to.

    Returns:
        A list of charge rate number entities, one per bucket, in
        temperature order.
    """
    from custom_components.hsem.utils.sensornames.controls import (
        get_charge_rate_number_entity_id,
        get_charge_rate_number_key,
        get_charge_rate_number_unique_id,
    )

    entities: list[HSEMChargeRateNumber] = []
    for bucket_name, _, _ in sorted(TEMP_BUCKETS, key=lambda b: _bucket_order(b[0])):
        desc = NumberEntityDescription(
            key=get_charge_rate_number_key(bucket_name),
            icon="mdi:thermometer-lines",
            translation_key=f"charge_rate_{bucket_name}",
        )
        entities.append(
            HSEMChargeRateNumber(
                hass,
                config_entry,
                desc,
                bucket_name=bucket_name,
                unique_id=get_charge_rate_number_unique_id(
                    config_entry.entry_id, bucket_name
                ),
                entity_id=get_charge_rate_number_entity_id(bucket_name),
            )
        )
    return entities
