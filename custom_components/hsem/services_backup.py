"""Backup, restore and debug-file services for HSEM (issue #1436).

- ``export_data`` — write the learned data and the redacted configuration to
  one JSON file under ``<config>/hsem_backups/``.
- ``import_data`` — validate a backup, save the current data first, replace
  the history files and reload the entry.
- :func:`async_write_removal_backup` — the backup written when the config
  entry is deleted.
- :func:`async_capture_learned_state`, :func:`async_stage_pending_import` and
  :func:`async_apply_pending_import` — the hooks ``async_unload_entry`` and
  ``async_setup_entry`` call, so learner state reaches the removal backup and
  an import survives a delayed start.
- :func:`async_write_debug_export` — the file and notification behind the
  ``write_file`` option of ``export_diagnostics``.

All file I/O runs in the executor; the payload logic lives in
:mod:`custom_components.hsem.utils.backup`.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import async_get_platforms

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.utils.backup import (
    LEARNER_AVERAGE_SENSORS,
    LEARNER_FORECAST_TRACKER,
    LEARNER_KEYS,
    LEARNER_SOLAR_CORRECTOR,
    PENDING_AVERAGES_KEY,
    REASON_BEFORE_IMPORT,
    REASON_DELETED,
    REASON_MANUAL,
    BackupError,
    build_backup_payload,
    build_issue_url,
    check_configuration_version,
    learned_state,
    read_backup_file,
    read_history_files,
    read_pending_import,
    remove_pending_import,
    restorable_options,
    write_backup_file,
    write_debug_file,
    write_history_files,
)
from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.integration_version import (
    async_get_hsem_integration_version,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.schema import vol

SERVICE_EXPORT_DATA = "export_data"
SERVICE_IMPORT_DATA = "import_data"

ATTR_FILENAME = "filename"
ATTR_RESTORE_CONFIGURATION = "restore_configuration"

SCHEMA_EXPORT_DATA = vol.Schema({})

SCHEMA_IMPORT_DATA = vol.Schema(
    {
        vol.Required(ATTR_FILENAME): vol.All(cv.string, vol.Length(min=1, max=255)),
        vol.Optional(ATTR_RESTORE_CONFIGURATION, default=False): cv.boolean,
    }
)

_DEBUG_NOTIFICATION_ID = "hsem_debug_export"

#: Forecast slots kept in a backup: the capacity of the coordinator's tracker.
_FORECAST_BACKUP_RECORDS = 2880

#: ``hass.data`` key: learner state kept from the last unload.
_CAPTURED_STATE_KEY = f"{DOMAIN}_captured_learned_state"

#: ``hass.data`` key: the learner sections the last setup loaded from an import.
_APPLIED_IMPORT_KEY = f"{DOMAIN}_applied_import"

#: ``hass.data`` key: the learner state of a pending import, read from the
#: file before the platforms are set up.
_STAGED_IMPORT_KEY = f"{DOMAIN}_staged_import"

#: ``hass.data`` key: the lock that lets one import run at a time.
_IMPORT_LOCK_KEY = f"{DOMAIN}_import_lock"

#: Entry states an import can start from. In any other state (a setup that
#: is retrying or failed, a failed unload) the entry cannot be unloaded or
#: set up again in a defined way.
_IMPORTABLE_STATES = (ConfigEntryState.LOADED, ConfigEntryState.NOT_LOADED)


def _config_dir(hass: HomeAssistant) -> Path:
    """Return the Home Assistant config directory."""
    return Path(hass.config.config_dir)


def _get_entry(hass: HomeAssistant) -> ConfigEntry:
    """Return the HSEM config entry, loaded or not.

    Raises:
        ServiceValidationError: When HSEM is not configured.
    """
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("HSEM is not configured: no config entry found.")
    return entries[0]


def _coordinator(entry: ConfigEntry) -> Any | None:
    """Return the running coordinator, or ``None`` when HSEM is unloaded."""
    return getattr(getattr(entry, "runtime_data", None), "coordinator", None)


def _average_sensors(hass: HomeAssistant) -> list[HSEMAvgSensor]:
    """Return every hourly average sensor HSEM has added."""
    return [
        entity
        for platform in async_get_platforms(hass, DOMAIN)
        for entity in platform.entities.values()
        if isinstance(entity, HSEMAvgSensor)
    ]


def collect_learned_state(
    hass: HomeAssistant, entry: ConfigEntry, now: datetime
) -> dict[str, dict[str, Any] | None]:
    """Return the learner state that lives in restore state, not in a file.

    Read from the running coordinator and sensors. When HSEM is unloaded the
    state captured by :func:`async_capture_learned_state` is used.
    """
    coordinator = _coordinator(entry)
    if coordinator is None:
        captured = hass.data.get(_CAPTURED_STATE_KEY)
        return dict(captured) if captured else dict.fromkeys(LEARNER_KEYS)

    corrector = getattr(coordinator, "_solar_corrector", None)
    tracker = getattr(coordinator, "_forecast_tracker", None)
    averages = {
        sensor.backup_key: {"measurements": sensor.export_measurements()}
        for sensor in _average_sensors(hass)
    }
    return {
        LEARNER_SOLAR_CORRECTOR: corrector.to_dict() if corrector is not None else None,
        LEARNER_FORECAST_TRACKER: (
            tracker.to_persistence_dict(now=now, max_records=_FORECAST_BACKUP_RECORDS)
            if tracker is not None
            else None
        ),
        LEARNER_AVERAGE_SENSORS: averages or None,
    }


def async_capture_learned_state(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Keep the learner state of an entry that is about to be unloaded.

    Home Assistant calls ``async_remove_entry`` after the unload, when the
    coordinator and the sensors are gone. The removal backup reads what is
    kept here. Never raises: a failure must not block the unload.
    """
    try:
        hass.data[_CAPTURED_STATE_KEY] = collect_learned_state(hass, entry, hsem_now())
    except Exception:  # noqa: BLE001 — the unload must go through
        _LOGGER.exception("HSEM could not capture its learned state before unload")


async def async_stage_pending_import(hass: HomeAssistant) -> None:
    """Read the learner state an import left behind, before the platforms load.

    Called from ``async_setup_entry`` before the platforms are forwarded. The
    samples of the hourly average sensors go to ``hass.data``, where each
    sensor takes its own when it is added: those sensors are created by
    another sensor, at a moment the setup does not control. Reading the file
    also undoes an import that was cut off mid-replace. Never raises.
    """
    storage_dir = _config_dir(hass) / ".storage"
    pending = await hass.async_add_executor_job(read_pending_import, storage_dir)
    hass.data.pop(_STAGED_IMPORT_KEY, None)
    hass.data.pop(PENDING_AVERAGES_KEY, None)
    if pending is None:
        return
    hass.data[_STAGED_IMPORT_KEY] = pending
    averages = pending.get(LEARNER_AVERAGE_SENSORS)
    if isinstance(averages, dict):
        hass.data[PENDING_AVERAGES_KEY] = dict(averages)


async def async_apply_pending_import(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Load the staged learner state into the coordinator, then delete the file.

    Called at the end of ``async_setup_entry``, after the sensors have
    restored their previous state, so the imported state wins. The file is
    only deleted here, at the end of a setup that succeeded: an import whose
    first start failed is applied again when Home Assistant retries. A
    section that cannot be loaded is logged and dropped; it is still in the
    backup file. Never raises.
    """
    # The learner state kept from the last unload is older than this setup.
    hass.data.pop(_CAPTURED_STATE_KEY, None)
    pending = hass.data.pop(_STAGED_IMPORT_KEY, None)
    coordinator = _coordinator(entry)
    if pending is None or coordinator is None:
        return
    now = hsem_now()
    applied: list[str] = []
    for key, target, load in (
        (
            LEARNER_SOLAR_CORRECTOR,
            coordinator._solar_corrector,
            lambda target, state: target.load_from_dict(state, restored_at=now),
        ),
        (
            LEARNER_FORECAST_TRACKER,
            coordinator._forecast_tracker,
            lambda target, state: target.load_from_dict(state),
        ),
    ):
        state = pending.get(key)
        if not isinstance(state, dict):
            continue
        try:
            load(target, state)
        except Exception:  # noqa: BLE001 — setup must go through
            _LOGGER.exception(
                "HSEM could not load the imported %s; it is still in the backup file",
                key,
            )
        else:
            applied.append(key)

    averages = pending.get(LEARNER_AVERAGE_SENSORS)
    waiting = hass.data.get(PENDING_AVERAGES_KEY, {})
    if isinstance(averages, dict) and len(waiting) < len(averages):
        applied.append(LEARNER_AVERAGE_SENSORS)

    await hass.async_add_executor_job(
        remove_pending_import, _config_dir(hass) / ".storage"
    )
    hass.data[_APPLIED_IMPORT_KEY] = applied
    # The confidence and accuracy sensors write the loaded state to their
    # restore attributes, so it survives the next restart.
    coordinator.async_update_listeners()
    _LOGGER.info("HSEM loaded imported learned state: %s", ", ".join(applied) or "none")


async def async_write_backup(
    hass: HomeAssistant, entry: ConfigEntry, reason: str
) -> Path:
    """Write a backup of the learned data and the redacted configuration.

    Args:
        hass: The Home Assistant instance.
        entry: The HSEM config entry.
        reason: One of the ``REASON_*`` constants; sets the file-name suffix.

    Returns:
        The path of the written file.

    Raises:
        BackupError: When the file cannot be written.
    """
    config_dir = _config_dir(hass)
    hsem_version = await async_get_hsem_integration_version(hass)
    history = await hass.async_add_executor_job(
        read_history_files, config_dir / ".storage"
    )
    created = hsem_now()
    payload = build_backup_payload(
        hsem_version=str(hsem_version),
        created=created,
        reason=reason,
        history=history,
        learned=collect_learned_state(hass, entry, created),
        data=entry.data,
        options=entry.options,
        entry_version=entry.version,
        entry_minor_version=entry.minor_version,
    )
    return await hass.async_add_executor_job(write_backup_file, config_dir, payload)


async def async_handle_export_data(call: ServiceCall) -> dict[str, Any]:
    """Write the learned data and the redacted configuration to one file.

    Args:
        call: The service call (schema is empty).

    Returns:
        ``{"backup_path": ...}``.

    Raises:
        ServiceValidationError: When HSEM is not configured.
        HomeAssistantError: When the file cannot be written.
    """
    entry = _get_entry(call.hass)
    try:
        path = await async_write_backup(call.hass, entry, REASON_MANUAL)
    except BackupError as err:
        raise HomeAssistantError(str(err)) from err
    _LOGGER.info("HSEM service: export_data wrote %s", path)
    return {"backup_path": str(path)}


async def async_handle_import_data(call: ServiceCall) -> dict[str, Any]:
    """Replace the learned data with the contents of a backup file.

    The file and the state of the entry are validated before anything
    changes. Then, with the coordinator's update lock held so no running
    cycle can write over the import, the current data is saved as
    ``..._before_import.json`` and the entry is unloaded. The history files
    are replaced all or nothing and the entry is set up again; the learner
    state of the backup is loaded at the end of that setup.

    Args:
        call: The service call with ``filename`` and optional
            ``restore_configuration``.

    Returns:
        The paths and what was restored.

    Only one import runs at a time: a second call is rejected while the first
    one is between its unload and its setup, where it would otherwise work on
    the same staged files.

    Raises:
        ServiceValidationError: When HSEM is not configured, another import
            is running, the entry is in a state an import cannot start from,
            or the backup is rejected. Nothing has changed.
        HomeAssistantError: When saving, replacing or reloading fails.
    """
    lock: asyncio.Lock = call.hass.data.setdefault(_IMPORT_LOCK_KEY, asyncio.Lock())
    if lock.locked():
        raise ServiceValidationError(
            "Another import is running, so nothing was imported. Wait until it "
            "has finished."
        )
    async with lock:
        return await _async_import(call)


async def _async_import(call: ServiceCall) -> dict[str, Any]:
    """Run one import. The caller holds the import lock."""
    hass = call.hass
    entry = _get_entry(hass)
    config_dir = _config_dir(hass)
    hsem_version = str(await async_get_hsem_integration_version(hass))
    restore_configuration: bool = call.data[ATTR_RESTORE_CONFIGURATION]

    if entry.state not in _IMPORTABLE_STATES:
        raise ServiceValidationError(
            f"HSEM is in the state '{entry.state.value}', so nothing was "
            "imported. Wait until HSEM has started, or fix the setup error, "
            "and try again."
        )
    try:
        payload = await hass.async_add_executor_job(
            lambda: read_backup_file(
                config_dir, call.data[ATTR_FILENAME], hsem_version=hsem_version
            )
        )
        if restore_configuration:
            check_configuration_version(payload, entry.version, entry.minor_version)
    except BackupError as err:
        raise ServiceValidationError(str(err)) from err

    was_loaded = entry.state is ConfigEntryState.LOADED
    coordinator = _coordinator(entry)
    hass.data.pop(_APPLIED_IMPORT_KEY, None)

    async def _save_and_unload() -> Path:
        try:
            path = await async_write_backup(hass, entry, REASON_BEFORE_IMPORT)
        except BackupError as err:
            raise HomeAssistantError(f"{err} Nothing was imported.") from err
        if was_loaded and not await hass.config_entries.async_unload(entry.entry_id):
            raise HomeAssistantError(
                "HSEM could not be unloaded, so nothing was imported. Restart "
                "Home Assistant and try again."
            )
        return path

    if coordinator is not None:
        async with coordinator.async_hold_updates():
            before_path = await _save_and_unload()
    else:
        before_path = await _save_and_unload()

    skipped: list[str] = []
    configuration_restored = False
    try:
        written = await hass.async_add_executor_job(
            write_history_files, config_dir / ".storage", payload
        )
        # What was kept from the unload belongs to the replaced history.
        hass.data.pop(_CAPTURED_STATE_KEY, None)
        if restore_configuration:
            options, skipped = restorable_options(payload)
            hass.config_entries.async_update_entry(
                entry, options={**entry.options, **options}
            )
            configuration_restored = True
    except BackupError as err:
        raise HomeAssistantError(
            f"{err} The data from before the import is in {before_path}."
        ) from err
    finally:
        if was_loaded:
            await hass.config_entries.async_setup(entry.entry_id)

    learned = sorted(learned_state(payload))
    result = {
        "imported_from": call.data[ATTR_FILENAME],
        "before_import_path": str(before_path),
        "history_files_restored": written,
        "learned_state_restored": sorted(hass.data.pop(_APPLIED_IMPORT_KEY, [])),
        "learned_state_pending": [],
        "configuration_restored": configuration_restored,
        "configuration_keys_skipped": skipped,
    }
    _LOGGER.info(
        "HSEM service: import_data restored %s from %s",
        ", ".join(written) or "no history files",
        call.data[ATTR_FILENAME],
    )
    if not was_loaded:
        # HSEM is not running: the learner state is loaded when it starts.
        result["learned_state_pending"] = learned
        return result
    if entry.state is not ConfigEntryState.LOADED:
        raise HomeAssistantError(
            "The data was imported, but HSEM did not start again. Check the "
            "log. The learned state is loaded when HSEM starts. The data from "
            f"before the import is in {before_path}."
        )
    return result


async def async_write_removal_backup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Write a ``..._deleted.json`` backup when the config entry is removed.

    Never raises: a failed backup must not block the removal.
    """
    try:
        path = await async_write_backup(hass, entry, REASON_DELETED)
    except Exception:  # noqa: BLE001 — removal must go through
        _LOGGER.exception("HSEM could not write a backup before removal")
        return
    _LOGGER.info("HSEM wrote a backup before removal: %s", path)


async def async_write_debug_export(
    hass: HomeAssistant, dump: dict[str, Any], hsem_version: str
) -> str:
    """Write a redacted diagnostics dump to a file and notify the user.

    The notification holds the file path and a new-issue link prefilled with
    the HSEM and Home Assistant versions. The link carries nothing else.

    Args:
        hass: The Home Assistant instance.
        dump: The redacted dump from ``build_diagnostics_dump``.
        hsem_version: The installed HSEM version.

    Returns:
        The path of the written file.

    Raises:
        HomeAssistantError: When the file cannot be written.
    """
    try:
        path = await hass.async_add_executor_job(
            write_debug_file, _config_dir(hass), dump, hsem_now()
        )
    except BackupError as err:
        raise HomeAssistantError(str(err)) from err

    issue_url = build_issue_url(hsem_version, HA_VERSION)
    try:
        await hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": _DEBUG_NOTIFICATION_ID,
                "title": "HSEM debug export",
                "message": (
                    f"The debug export was written to `{path}`.\n\n"
                    f"[Open a new issue]({issue_url}) and attach the file. "
                    "The link holds the HSEM and Home Assistant versions only."
                ),
            },
            blocking=True,
        )
    except HomeAssistantError:
        _LOGGER.warning("HSEM could not create the debug export notification")
    return str(path)
