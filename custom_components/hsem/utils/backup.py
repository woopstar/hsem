"""Backup payloads for HSEM's learned data and the debug export (issue #1436).

Single responsibility: build, validate, read and write the one-file JSON
backup behind ``hsem.export_data`` / ``hsem.import_data``, and the file
behind the debug export of ``hsem.export_diagnostics``.

Nothing here touches Home Assistant. The functions that read or write files
block, so callers run them in the executor.

A backup never holds key material. The config entry holds only *paths* to
OCPP certificates, and those are redacted together with any key that looks
like a secret; the certificate files themselves are never read. Entity IDs
are kept, so a backup can restore the full configuration on another Home
Assistant; the debug export, which is meant to be posted, redacts them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from packaging.version import InvalidVersion, Version

from custom_components.hsem.utils.diagnostics import REDACTED
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

#: Learned state that is not a file: it lives in the restore state of HSEM's
#: sensors. A backup carries each under its own key.
LEARNER_SOLAR_CORRECTOR = "solar_corrector"
LEARNER_FORECAST_TRACKER = "forecast_tracker"
LEARNER_AVERAGE_SENSORS = "average_sensors"
LEARNER_KEYS: tuple[str, ...] = (
    LEARNER_SOLAR_CORRECTOR,
    LEARNER_FORECAST_TRACKER,
    LEARNER_AVERAGE_SENSORS,
)

#: File under ``<config>/.storage`` that holds imported learner state until
#: HSEM has started and loaded it. It survives a setup that Home Assistant
#: retries later.
PENDING_IMPORT_FILE = "hsem_pending_import.json"

#: Why a backup was written; also the file-name suffix.
REASON_MANUAL = "manual"
REASON_BEFORE_IMPORT = "before_import"
REASON_DELETED = "deleted"

#: A backup holds up to five history files, each capped at 16 MiB on read.
_MAX_BACKUP_FILE_BYTES = 96 * 1024 * 1024

#: Key substrings that mark a secret wherever they appear, on top of
#: ``redact_dict``. ``cpid`` is the OCPP charge point identity: without TLS it
#: is all a client needs to connect as the charger.
_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "private",
    "passphrase",
    "passwd",
    "credential",
    "auth",
    "cpid",
)

#: Key substrings redacted in a backup. On top of the secrets: tokens and
#: passwords (the names ``redact_dict`` knows; ``_key`` covers ``api_key`` and
#: ``access_key``), and the OCPP TLS options, which are file paths to
#: certificates and private keys. ``redact_dict`` itself is not used, since it
#: also replaces every entity ID. Not applied to the debug export, whose
#: ``ocpp_tls`` section holds certificate expiry dates and fingerprints.
_KEY_MATERIAL_SUBSTRINGS: tuple[str, ...] = (
    *_SECRET_SUBSTRINGS,
    "token",
    "password",
    "secret",
    "client_id",
    "cert",
    "_key",
    "_ca_",
)

#: Start of a PEM block; a value holding one is key material wherever it sits.
_PEM_MARKER = "-----BEGIN"

ISSUE_URL = "https://github.com/woopstar/hsem/issues/new"


class BackupError(Exception):
    """A backup file cannot be used. The message is safe to show the user."""


def _redact_key_material(
    data: Mapping[str, Any], substrings: tuple[str, ...]
) -> dict[str, Any]:
    """Redact fields named like a secret, and any value holding a PEM block."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        lowered = str(key).lower()
        if any(part in lowered for part in substrings):
            result[key] = REDACTED
        else:
            result[key] = _redact_item(value, substrings)
    return result


def _redact_item(value: Any, substrings: tuple[str, ...]) -> Any:
    """Redact one value, descending into dicts and lists."""
    if isinstance(value, dict):
        return _redact_key_material(value, substrings)
    if isinstance(value, list):
        return [_redact_item(item, substrings) for item in value]
    if isinstance(value, str) and _PEM_MARKER in value:
        return REDACTED
    return value


def redact_configuration(values: Mapping[str, Any]) -> dict[str, Any]:
    """Return config entry values that are safe to write to a backup file.

    Redacts tokens, passwords, charge point IDs, certificate and key fields
    and any PEM block. Entity IDs are kept: without them a backup could not
    restore the configuration on another Home Assistant.

    Args:
        values: ``entry.data`` or ``entry.options``.

    Returns:
        A redacted copy.
    """
    return _redact_key_material(values, _KEY_MATERIAL_SUBSTRINGS)


def _contains_redacted(value: Any) -> bool:
    """Return whether *value* is, or holds, a redaction placeholder."""
    if isinstance(value, dict):
        return any(_contains_redacted(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_redacted(item) for item in value)
    return bool(value == REDACTED)


def restorable_options(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Split a backup's options into what can be restored and what cannot.

    A redacted value (a secret, a certificate path) cannot be restored;
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
    learned: Mapping[str, dict[str, Any] | None],
    data: Mapping[str, Any],
    options: Mapping[str, Any],
    entry_version: int,
    entry_minor_version: int,
) -> dict[str, Any]:
    """Build the backup payload.

    Args:
        hsem_version: HSEM version writing the backup.
        created: When the backup is taken.
        reason: One of the ``REASON_*`` constants.
        history: History file contents from :func:`read_history_files`.
        learned: Learner state by ``LEARNER_KEYS``; ``None`` or absent when
            it could not be read.
        data: ``entry.data``; redacted here.
        options: ``entry.options``; redacted here.
        entry_version: The config entry version the options belong to.
        entry_minor_version: The config entry minor version.

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
        **{key: learned.get(key) for key in LEARNER_KEYS},
        "configuration": {
            "entry_version": entry_version,
            "entry_minor_version": entry_minor_version,
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

    for key in LEARNER_KEYS:
        section = payload.get(key)
        if section is not None and not isinstance(section, dict):
            raise BackupError(f"The backup holds an invalid {key} section.")

    return payload


def check_configuration_version(payload: Mapping[str, Any], entry_version: int) -> None:
    """Check that the backup's options fit the installed config entry version.

    Options from another entry version may use keys a migration has renamed
    or removed, and the entry migration is not written to run on a partly
    redacted options dict. Such a backup still restores the learned data.

    Raises:
        BackupError: When the backup states no entry version or another one.
    """
    configuration = payload.get("configuration")
    backup_version = (
        configuration.get("entry_version") if isinstance(configuration, dict) else None
    )
    if isinstance(backup_version, bool) or backup_version != entry_version:
        raise BackupError(
            f"The configuration in the backup has config entry version "
            f"{backup_version!r}; this HSEM uses version {entry_version}. "
            "Import again without restore_configuration to restore the "
            "learned data only."
        )


def resolve_backup_path(config_dir: Path, filename: str) -> Path:
    """Return the path of a backup file by its name.

    Only a plain file name inside ``<config>/hsem_backups/`` is accepted, so
    the service cannot be pointed at another file on the host.

    Raises:
        BackupError: When *filename* is not a plain ``.json`` file name.
    """
    if (
        not filename
        or not filename.isprintable()
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


def learned_state(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return the learner sections a validated backup carries."""
    return {
        key: section
        for key in LEARNER_KEYS
        if isinstance(section := payload.get(key), dict)
    }


def write_history_files(storage_dir: Path, payload: Mapping[str, Any]) -> list[str]:
    """Replace the history files with those in a validated backup. Blocking.

    All or nothing: every file is first written next to its target, and the
    targets are only replaced once all of them are on disk. If a replace
    fails, the files already replaced are put back. A history file the backup
    does not carry is left as it is.

    The learner state of the backup goes to ``PENDING_IMPORT_FILE`` in the
    same step; HSEM loads it when it starts.

    Args:
        storage_dir: ``<config>/.storage``.
        payload: A validated backup payload.

    Returns:
        The names of the history files that were written.

    Raises:
        BackupError: When a file cannot be written. Nothing has changed.
    """
    history = payload["history"]
    contents: dict[str, Mapping[str, Any]] = {
        name: history[name] for name in HISTORY_FILES if history.get(name) is not None
    }
    written = list(contents)
    learned = learned_state(payload)
    if learned:
        contents[PENDING_IMPORT_FILE] = learned

    staged: list[tuple[Path, Path, Path]] = []
    try:
        for name, content in contents.items():
            target = storage_dir / name
            new = storage_dir / f"{name}.import_new"
            if not write_json_history_file(new, content):
                raise BackupError(f"Could not write {target}.")
            staged.append((target, new, storage_dir / f"{name}.import_old"))
        _swap_in(staged)
    finally:
        for _target, new, old in staged:
            for leftover in (new, old):
                with suppress(OSError):
                    leftover.unlink()
    return written


def _swap_in(staged: list[tuple[Path, Path, Path]]) -> None:
    """Move every staged file onto its target, or put all targets back.

    Raises:
        BackupError: When a move fails. The targets are as they were.
    """
    done: list[tuple[Path, Path, bool]] = []
    try:
        for target, new, old in staged:
            had_target = target.exists()
            if had_target:
                os.replace(target, old)
            done.append((target, old, had_target))
            os.replace(new, target)
    except OSError as err:
        for target, old, had_target in reversed(done):
            with suppress(OSError):
                if had_target:
                    os.replace(old, target)
                else:
                    target.unlink()
        raise BackupError(
            f"Could not replace the history files ({err}). Nothing was imported."
        ) from err


def read_pending_import(storage_dir: Path) -> dict[str, Any] | None:
    """Read the learner state an import left for the next start. Blocking."""
    return read_json_history_file(
        storage_dir / PENDING_IMPORT_FILE, max_bytes=_MAX_BACKUP_FILE_BYTES
    )


def remove_pending_import(storage_dir: Path) -> None:
    """Delete the pending learner state once it is loaded. Blocking."""
    with suppress(OSError):
        (storage_dir / PENDING_IMPORT_FILE).unlink()


def redact_debug_dump(dump: Mapping[str, Any]) -> dict[str, Any]:
    """Redact secrets a diagnostics dump may still hold.

    The dump is already passed through ``redact_dict``. This adds the secret
    field names and PEM blocks, so the file is safe to attach to an issue.
    """
    return _redact_key_material(dump, _SECRET_SUBSTRINGS)


def write_debug_file(
    config_dir: Path, dump: Mapping[str, Any], created: datetime
) -> Path:
    """Write a redacted diagnostics dump to ``<config>/hsem_debug/``. Blocking.

    Raises:
        BackupError: When the file cannot be written.
    """
    name = f"hsem_debug_{created.strftime('%Y%m%d_%H%M%S')}.json"
    path = config_dir / DEBUG_DIR_NAME / name
    if not write_json_history_file(path, redact_debug_dump(dump), indent=2):
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
