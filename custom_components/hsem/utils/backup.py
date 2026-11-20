"""Backup payloads for HSEM's learned data and the debug export (issue #1436).

Single responsibility: build, validate, read and write the one-file JSON
backup behind ``hsem.export_data`` / ``hsem.import_data``, and the file
behind the debug export of ``hsem.export_diagnostics``.

Nothing here touches Home Assistant. The functions that read or write files
block, so callers run them in the executor.

A backup never holds key material. The config entry holds only *paths* to
OCPP certificates, and those are redacted together with entity IDs and any
key that looks like a secret; the certificate files themselves are never
read.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from packaging.version import InvalidVersion, Version

from custom_components.hsem.utils.diagnostics import REDACTED, redact_dict
from custom_components.hsem.utils.persistence import (
    read_json_history_file,
    write_json_history_file,
)

#: Marker that identifies a file as an HSEM backup.
BACKUP_SCHEMA = "hsem_backup"

#: Version of the backup layout. Raise it when the layout changes in a way an
#: older HSEM cannot read.
BACKUP_SCHEMA_VERSION = 1

#: Folder under the Home Assistant config directory that holds backups.
BACKUP_DIR_NAME = "hsem_backups"

#: Folder under the Home Assistant config directory that holds debug exports.
DEBUG_DIR_NAME = "hsem_debug"

#: Every history file HSEM persists under ``<config>/.storage``. The solar
#: corrector is not a file (it lives in the confidence sensor's restore
#: state), so a backup carries it under its own key.
HISTORY_FILES: tuple[str, ...] = (
    "hsem_prediction_history.json",
    "hsem_financial_history.json",
    "hsem_savings_history.json",
    "hsem_daily_history.json",
    "hsem_performance_history.json",
)

#: Why a backup was written; also the file-name suffix.
REASON_MANUAL = "manual"
REASON_BEFORE_IMPORT = "before_import"
REASON_DELETED = "deleted"

#: A backup holds up to five history files, each capped at 16 MiB on read.
_MAX_BACKUP_FILE_BYTES = 96 * 1024 * 1024

#: Key substrings redacted on top of ``redact_dict``: OCPP TLS options are
#: file paths to certificates and private keys.
_KEY_MATERIAL_SUBSTRINGS: tuple[str, ...] = ("cert", "_key", "_ca_", "private")

#: Start of a PEM block; a value holding one is key material wherever it sits.
_PEM_MARKER = "-----BEGIN"

ISSUE_URL = "https://github.com/woopstar/hsem/issues/new"


class BackupError(Exception):
    """A backup file cannot be used. The message is safe to show the user."""


def _redact_key_material(data: dict[str, Any]) -> dict[str, Any]:
    """Redact certificate and key fields, and any value holding a PEM block."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        lowered = key.lower()
        if any(part in lowered for part in _KEY_MATERIAL_SUBSTRINGS):
            result[key] = REDACTED
        elif isinstance(value, dict):
            result[key] = _redact_key_material(value)
        elif isinstance(value, list):
            result[key] = [_redact_item(item) for item in value]
        else:
            result[key] = _redact_item(value)
    return result


def _redact_item(value: Any) -> Any:
    """Redact one list item or scalar."""
    if isinstance(value, dict):
        return _redact_key_material(value)
    if isinstance(value, str) and _PEM_MARKER in value:
        return REDACTED
    return value


def redact_configuration(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return config entry values that are safe to write to a backup file.

    Applies ``redact_dict`` (entity IDs, tokens, passwords) and then redacts
    certificate and key fields.

    Args:
        values: ``entry.data`` or ``entry.options``.

    Returns:
        A redacted copy.
    """
    return _redact_key_material(redact_dict(dict(values)))


def _contains_redacted(value: Any) -> bool:
    """Return whether *value* is, or holds, a redaction placeholder."""
    if isinstance(value, dict):
        return any(_contains_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted(item) for item in value)
    return bool(value == REDACTED)


def restorable_options(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split a backup's options into what can be restored and what cannot.

    A redacted value (an entity ID, a certificate path) cannot be restored;
    the entry keeps its current value for that key.

    Args:
        payload: A validated backup payload.

    Returns:
        The options to write, and the sorted keys that were skipped.
    """
    configuration = payload.get("configuration")
    options = configuration.get("options") if isinstance(configuration, dict) else None
    if not isinstance(options, dict):
        return {}, []
    restorable: dict[str, Any] = {}
    skipped: list[str] = []
    for key, value in options.items():
        if _contains_redacted(value):
            skipped.append(key)
        else:
            restorable[key] = value
    return restorable, sorted(skipped)


def read_history_files(storage_dir: Path) -> dict[str, dict[str, Any] | None]:
    """Read every HSEM history file. Blocking.

    Args:
        storage_dir: ``<config>/.storage``.

    Returns:
        File name to parsed content, ``None`` for a missing or unreadable file.
    """
    return {name: read_json_history_file(storage_dir / name) for name in HISTORY_FILES}


def build_backup_payload(
    *,
    hsem_version: str,
    created: datetime,
    reason: str,
    history: Mapping[str, dict[str, Any] | None],
    solar_corrector: dict[str, Any] | None,
    data: Mapping[str, Any],
    options: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the backup payload.

    Args:
        hsem_version: HSEM version writing the backup.
        created: When the backup is taken.
        reason: One of the ``REASON_*`` constants.
        history: History file contents from :func:`read_history_files`.
        solar_corrector: ``SolarForecastCorrector.to_dict()`` or ``None``.
        data: ``entry.data``; redacted here.
        options: ``entry.options``; redacted here.

    Returns:
        A JSON-serialisable payload.
    """
    return {
        "schema": BACKUP_SCHEMA,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "hsem_version": hsem_version,
        "created": created.isoformat(),
        "reason": reason,
        "history": {name: history.get(name) for name in HISTORY_FILES},
        "solar_corrector": solar_corrector,
        "configuration": {
            "data": redact_configuration(data),
            "options": redact_configuration(options),
        },
    }


def backup_file_name(created: datetime, reason: str) -> str:
    """Return the backup file name for *created* and *reason*."""
    stamp = created.strftime("%Y%m%d_%H%M%S")
    suffix = "" if reason == REASON_MANUAL else f"_{reason}"
    return f"hsem_backup_{stamp}{suffix}.json"


def write_backup_file(config_dir: Path, payload: Mapping[str, Any]) -> Path:
    """Write *payload* to ``<config>/hsem_backups/``. Blocking.

    Args:
        config_dir: The Home Assistant config directory.
        payload: A payload from :func:`build_backup_payload`.

    Returns:
        The path of the written file.

    Raises:
        BackupError: When the file cannot be written.
    """
    created = datetime.fromisoformat(str(payload["created"]))
    path = (
        config_dir / BACKUP_DIR_NAME / backup_file_name(created, str(payload["reason"]))
    )
    if not write_json_history_file(path, payload, indent=2):
        raise BackupError(f"Could not write the HSEM backup file {path}.")
    return path


def _parse_version(value: Any) -> Version | None:
    """Parse a version string, ``None`` when it is not a valid version."""
    try:
        return Version(str(value))
    except InvalidVersion:
        return None


def validate_backup_payload(payload: Any, *, hsem_version: str) -> dict[str, Any]:
    """Check that *payload* is a backup this HSEM version can import.

    Args:
        payload: The parsed backup file.
        hsem_version: The installed HSEM version.

    Returns:
        The payload, unchanged.

    Raises:
        BackupError: On an unknown schema, a newer schema or HSEM version, or
            a malformed payload.
    """
    if not isinstance(payload, dict) or payload.get("schema") != BACKUP_SCHEMA:
        raise BackupError("The file is not an HSEM backup (unknown schema).")

    schema_version = payload.get("schema_version")
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or not 1 <= schema_version <= BACKUP_SCHEMA_VERSION
    ):
        raise BackupError(
            f"Backup schema version {schema_version!r} is not supported; this "
            f"HSEM reads schema versions up to {BACKUP_SCHEMA_VERSION}. "
            "Update HSEM before importing."
        )

    backup_version = _parse_version(payload.get("hsem_version"))
    if backup_version is None:
        raise BackupError("The backup does not state a valid HSEM version.")
    installed = _parse_version(hsem_version)
    if installed is not None and backup_version > installed:
        raise BackupError(
            f"The backup was written by HSEM {backup_version}, which is newer "
            f"than the installed {installed}. Update HSEM before importing."
        )

    history = payload.get("history")
    if not isinstance(history, dict) or any(
        value is not None and not isinstance(value, dict) for value in history.values()
    ):
        raise BackupError("The backup holds no valid history section.")

    solar_corrector = payload.get("solar_corrector")
    if solar_corrector is not None and not isinstance(solar_corrector, dict):
        raise BackupError("The backup holds an invalid solar corrector section.")

    return payload


def resolve_backup_path(config_dir: Path, filename: str) -> Path:
    """Return the path of a backup file by its name.

    Only a plain file name inside ``<config>/hsem_backups/`` is accepted, so
    the service cannot be pointed at another file on the host.

    Raises:
        BackupError: When *filename* is not a plain ``.json`` file name.
    """
    if (
        not filename
        or Path(filename).name != filename
        or filename.startswith(".")
        or not filename.endswith(".json")
    ):
        raise BackupError(
            f"'{filename}' is not a backup file name. Give the name of a .json "
            f"file in the {BACKUP_DIR_NAME} folder, without a path."
        )
    return config_dir / BACKUP_DIR_NAME / filename


def read_backup_file(config_dir: Path, filename: str, *, hsem_version: str) -> dict:
    """Read and validate a backup file. Blocking.

    Args:
        config_dir: The Home Assistant config directory.
        filename: Name of a file in ``<config>/hsem_backups/``.
        hsem_version: The installed HSEM version.

    Returns:
        The validated payload.

    Raises:
        BackupError: When the file is missing, unreadable or not importable.
    """
    path = resolve_backup_path(config_dir, filename)
    payload = read_json_history_file(path, max_bytes=_MAX_BACKUP_FILE_BYTES)
    if payload is None:
        raise BackupError(f"Could not read {path}: missing, too large or not JSON.")
    return validate_backup_payload(payload, hsem_version=hsem_version)


def write_history_files(storage_dir: Path, payload: Mapping[str, Any]) -> list[str]:
    """Replace the history files with those in a validated backup. Blocking.

    A history file the backup does not carry is left as it is.

    Args:
        storage_dir: ``<config>/.storage``.
        payload: A validated backup payload.

    Returns:
        The names of the files that were written.

    Raises:
        BackupError: When a file cannot be written.
    """
    history = payload["history"]
    written: list[str] = []
    for name in HISTORY_FILES:
        content = history.get(name)
        if content is None:
            continue
        if not write_json_history_file(storage_dir / name, content):
            raise BackupError(f"Could not write {storage_dir / name}.")
        written.append(name)
    return written


def write_debug_file(
    config_dir: Path, dump: Mapping[str, Any], created: datetime
) -> Path:
    """Write a redacted diagnostics dump to ``<config>/hsem_debug/``. Blocking.

    Raises:
        BackupError: When the file cannot be written.
    """
    name = f"hsem_debug_{created.strftime('%Y%m%d_%H%M%S')}.json"
    path = config_dir / DEBUG_DIR_NAME / name
    if not write_json_history_file(path, dump, indent=2):
        raise BackupError(f"Could not write the HSEM debug file {path}.")
    return path


def build_issue_url(hsem_version: str, ha_version: str) -> str:
    """Return a new-issue link prefilled with the two versions only.

    The link carries no user data: the debug file is attached by hand.
    """
    body = (
        "**Versions**\n\n"
        f"- HSEM: {hsem_version}\n"
        f"- Home Assistant: {ha_version}\n\n"
        "**What happened**\n\n\n"
        "**What did you expect**\n\n\n"
        "**Debug export**\n\n"
        "Attach the file from the `hsem_debug` folder here.\n"
    )
    query = urlencode({"labels": "bug", "title": "fix: ", "body": body})
    return f"{ISSUE_URL}?{query}"
