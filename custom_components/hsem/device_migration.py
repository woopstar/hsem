"""One-time entity-registry migration for the HSEM device split (issue #875).

Before this migration, every HSEM entity lived on a single Home Assistant
device (``(DOMAIN, entry_id)``). :mod:`custom_components.hsem.devices` now
splits entities across 7 devices, and :class:`HSEMEntity.device_info`
(`entity.py`) already resolves every *newly added* entity to its correct
device. Home Assistant's own entity-platform setup
(``EntityPlatform._async_add_entity`` -> ``entity_registry.async_get_or_create``)
already re-attaches an existing entity (matched by ``unique_id``) to a
changed ``device_info`` on every setup, so this migration mostly exists to:

- Make the device reassignment explicit, deterministic, and independently
  testable rather than relying on an HA-internal implementation detail.
- Provide the ``entity_registry.async_update_entity(..., new_entity_id=...)``
  primitive the issue calls for, so any *future* entity_id change in this
  area is guaranteed to move long-term statistics with it (HA's recorder
  listens to the entity-registry rename event and renames the matching
  ``statistic_id`` in lockstep). No entity in this migration actually needs
  an entity_id rename yet -- ``unique_id`` and ``entity_id`` getters were
  deliberately kept frozen; only entity *names* and device attachment
  changed -- but the rename path is implemented and tested so it is ready
  the moment it is needed.

The migration NEVER touches ``unique_id`` -- that is Home Assistant's true
identity key -- and is gated by a migration-version flag stored in
``entry.data`` so it runs exactly once per config entry.
"""

from __future__ import annotations

import logging

import homeassistant.helpers.device_registry as dr
import homeassistant.helpers.entity_registry as er
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.devices import HSEMDevice, get_device_info

_LOGGER = logging.getLogger(__name__)

#: Bumped whenever the device-split migration logic changes in a way that
#: needs to re-run for existing installs.
DEVICE_MIGRATION_VERSION = 1

#: Key stored in ``entry.data`` recording the migration version already
#: applied to this config entry. Distinct from ``entry.version`` /
#: ``entry.minor_version``, which gate config-schema migrations
#: (``async_migrate_entry``), not entity-registry bookkeeping.
DEVICE_MIGRATION_DATA_KEY = "hsem_device_migration_version"

#: Optional explicit entity_id renames, keyed by the entity's ``unique_id``.
#: Empty today -- see the module docstring -- but kept as a real, exercised
#: code path (see tests) for any future entity that needs one.
_ENTITY_ID_RENAMES: dict[str, str] = {}


def classify_entity_device(unique_id: str) -> HSEMDevice:
    """Return the target :class:`HSEMDevice` for a stored ``unique_id``.

    Pure, offline classification based only on the frozen ``unique_id``
    string (never re-instantiates entities), so it can run against
    registry entries alone during migration. Mirrors the live
    ``_hsem_device`` assignment in every entity class 1:1 -- see
    ``custom_components/hsem/devices.py`` and the per-class
    ``self._hsem_device = ...`` assignments for the authoritative mapping.

    Args:
        unique_id: The entity's stable, never-changing unique ID.

    Returns:
        The device the entity should be attached to. Defaults to
        ``CONTROLLER`` -- the pre-split single device -- for anything that
        does not match a more specific subsystem.
    """
    uid = unique_id.lower()

    # EV Secondary — anything explicitly tagged "second" for EV/OCPP.
    if "ev_second_" in uid or ("ocpp_charger" in uid and uid.endswith("_second")):
        return HSEMDevice.EV_SECONDARY

    # EV Primary — remaining EV/OCPP entities.
    if "ev_" in uid or "ocpp_charger" in uid:
        return HSEMDevice.EV_PRIMARY

    # Hourly Consumption Profile — the 168 per-hour-block entities.
    if "house_consumption_" in uid:
        return HSEMDevice.HOURLY_CONSUMPTION

    # Financial.
    if any(
        marker in uid
        for marker in (
            "export_income_sensor",
            "import_cost_sensor",
            "net_grid_balance_sensor",
            "savings_tracker_sensor",
        )
    ):
        return HSEMDevice.FINANCIAL

    # Forecast.
    if any(
        marker in uid
        for marker in (
            "forecast_accuracy_sensor",
            "solar_confidence_sensor",
            "prediction_accuracy_sensor",
            "solcast_likelihood",
        )
    ):
        return HSEMDevice.FORECAST

    # Battery & Energy.
    if any(
        marker in uid
        for marker in (
            "battery_soc_sensor",
            "effective_discharge_floor_sensor",
            "net_consumption_sensor",
            "pv_curtailment_sensor",
            "battery_charge_efficiency",
            "battery_discharge_efficiency",
            "batteries_schedule",
            "dynamic_discharge_floor",
        )
    ):
        return HSEMDevice.BATTERY_ENERGY

    # Everything else (working mode, degraded mode, read-only, hardware
    # writes, missing entities, force mode, last/next updated, update
    # interval, applier status, plan explanation, daily plan vs actual,
    # recommendation interval, and generic switches) keeps its pre-split
    # Controller identity — no migration needed for these.
    return HSEMDevice.CONTROLLER


async def async_migrate_devices(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Run the one-time device-split migration for ``entry``.

    Idempotent and gated by :data:`DEVICE_MIGRATION_DATA_KEY`: a second call
    for the same (already-migrated) config entry is a no-op that does not
    touch the entity or device registries at all.

    Args:
        hass: The Home Assistant instance.
        entry: The HSEM config entry to migrate.
    """
    if entry.data.get(DEVICE_MIGRATION_DATA_KEY, 0) >= DEVICE_MIGRATION_VERSION:
        return

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)

    migrated = 0
    for entity_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
        if entity_entry.unique_id is None:  # pragma: no cover — HA always sets this
            continue

        target_device = classify_entity_device(entity_entry.unique_id)
        device_info = get_device_info(entry.entry_id, target_device)
        device = device_reg.async_get_or_create(
            config_entry_id=entry.entry_id, **device_info
        )

        device_id_update = device.id if entity_entry.device_id != device.id else None

        new_entity_id = _ENTITY_ID_RENAMES.get(entity_entry.unique_id)
        if new_entity_id == entity_entry.entity_id:
            new_entity_id = None

        if device_id_update is None and new_entity_id is None:
            continue

        _LOGGER.debug(
            "Migrating HSEM entity %s to device %s (device_id=%s, new_entity_id=%s)",
            entity_entry.entity_id,
            target_device.value,
            device_id_update,
            new_entity_id,
        )
        # entity_registry.async_update_entity() keyword-arg overloads don't
        # accept a splatted dict (mypy: incompatible with the Mapping/bool/int/
        # ... union of every parameter type), so branch explicitly instead.
        if device_id_update is not None and new_entity_id is not None:
            entity_reg.async_update_entity(
                entity_entry.entity_id,
                device_id=device_id_update,
                new_entity_id=new_entity_id,
            )
        elif device_id_update is not None:
            entity_reg.async_update_entity(
                entity_entry.entity_id, device_id=device_id_update
            )
        elif new_entity_id is not None:
            entity_reg.async_update_entity(
                entity_entry.entity_id, new_entity_id=new_entity_id
            )
        migrated += 1

    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, DEVICE_MIGRATION_DATA_KEY: DEVICE_MIGRATION_VERSION},
    )
    _LOGGER.info(
        "HSEM device-split migration complete for entry %s (%d entities moved)",
        entry.entry_id,
        migrated,
    )
