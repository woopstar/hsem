"""Backup, restore and debug-file services for HSEM (issue #1436).

- ``export_data`` — write the learned data and the redacted configuration to
  one JSON file under ``<config>/hsem_backups/``.
- ``import_data`` — validate a backup, save the current data first, replace
  the history files and reload the entry.
- :func:`async_write_removal_backup` — the backup written when the config
  entry is deleted.
- :func:`async_write_debug_export` — the file and notification behind the
  ``write_file`` option of ``export_diagnostics``.

All file I/O runs in the executor; the payload logic lives in
:mod:`custom_components.hsem.utils.backup`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import homeassistant.helpers.config_validation as cv
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.backup import (
    REASON_BEFORE_IMPORT,
    REASON_DELETED,
    REASON_MANUAL,
    BackupError,
    build_backup_payload,
    build_issue_url,
    read_backup_file,
    read_history_files,
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


def _solar_corrector(entry: ConfigEntry) -> Any | None:
    """Return the running solar corrector, or ``None`` when HSEM is unloaded."""
    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime_data, "coordinator", None)
    return getattr(coordinator, "_solar_corrector", None)


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
    corrector = _solar_corrector(entry)
    payload = build_backup_payload(
        hsem_version=str(hsem_version),
        created=hsem_now(),
        reason=reason,
        history=history,
        solar_corrector=corrector.to_dict() if corrector is not None else None,
        data=entry.data,
        options=entry.options,
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

    The file is validated before anything changes. The current data is then
    saved as ``..._before_import.json``, the entry is unloaded so no cycle
    writes over the imported files, the history files are replaced and the
    entry is set up again.

    Args:
        call: The service call with ``filename`` and optional
            ``restore_configuration``.

    Returns:
        The paths and what was restored.

    Raises:
        ServiceValidationError: When HSEM is not configured or the backup is
            rejected. Nothing has changed.
        HomeAssistantError: When saving, replacing or reloading fails.
    """
    hass = call.hass
    entry = _get_entry(hass)
    config_dir = _config_dir(hass)
    hsem_version = str(await async_get_hsem_integration_version(hass))

    try:
        payload = await hass.async_add_executor_job(
            lambda: read_backup_file(
                config_dir, call.data[ATTR_FILENAME], hsem_version=hsem_version
            )
        )
    except BackupError as err:
        raise ServiceValidationError(str(err)) from err

    try:
        before_path = await async_write_backup(hass, entry, REASON_BEFORE_IMPORT)
    except BackupError as err:
        raise HomeAssistantError(f"{err} Nothing was imported.") from err

    if entry.state is ConfigEntryState.LOADED and not (
        await hass.config_entries.async_unload(entry.entry_id)
    ):
        raise HomeAssistantError(
            "HSEM could not be unloaded, so nothing was imported. Restart Home "
            "Assistant and try again."
        )

    skipped: list[str] = []
    configuration_restored = False
    try:
        written = await hass.async_add_executor_job(
            write_history_files, config_dir / ".storage", payload
        )
        if call.data[ATTR_RESTORE_CONFIGURATION]:
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
        await hass.config_entries.async_setup(entry.entry_id)

    if entry.state is not ConfigEntryState.LOADED:
        raise HomeAssistantError(
            "The data was imported, but HSEM did not start again. Check the "
            f"log. The data from before the import is in {before_path}."
        )

    corrector = _solar_corrector(entry)
    corrector_state = payload.get("solar_corrector")
    solar_corrector_restored = corrector is not None and corrector_state is not None
    if corrector is not None and corrector_state is not None:
        corrector.load_from_dict(corrector_state, restored_at=hsem_now())

    _LOGGER.info(
        "HSEM service: import_data restored %s from %s",
        ", ".join(written) or "no history files",
        call.data[ATTR_FILENAME],
    )
    return {
        "imported_from": call.data[ATTR_FILENAME],
        "before_import_path": str(before_path),
        "history_files_restored": written,
        "solar_corrector_restored": solar_corrector_restored,
        "configuration_restored": configuration_restored,
        "configuration_keys_skipped": skipped,
    }


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
