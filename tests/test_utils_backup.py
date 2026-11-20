"""Tests for the backup payload helpers (issue #1436)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest

from custom_components.hsem.utils import backup
from custom_components.hsem.utils.backup import (
    BACKUP_SCHEMA,
    BACKUP_SCHEMA_VERSION,
    HISTORY_FILES,
    REASON_BEFORE_IMPORT,
    REASON_DELETED,
    REASON_MANUAL,
    BackupError,
    backup_file_name,
    build_backup_payload,
    build_issue_url,
    check_configuration_version,
    read_backup_file,
    read_history_files,
    read_pending_import,
    redact_configuration,
    redact_debug_dump,
    remove_pending_import,
    resolve_backup_path,
    restorable_options,
    validate_backup_payload,
    write_backup_file,
    write_debug_file,
    write_history_files,
)
from custom_components.hsem.utils.diagnostics import REDACTED

CREATED = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG\n-----END PRIVATE KEY-----"


def _payload(**overrides: Any) -> dict[str, Any]:
    """Return a valid backup payload."""
    payload = build_backup_payload(
        hsem_version="7.1.0",
        created=CREATED,
        reason=REASON_MANUAL,
        history={"hsem_savings_history.json": {"actual_savings": 12.5}},
        learned={"solar_corrector": {"schema_version": 3}},
        data={"device_name": "HSEM"},
        options={"hsem_read_only": True},
        entry_version=4,
        entry_minor_version=1,
    )
    payload.update(overrides)
    return payload


def test_payload_carries_schema_version_and_every_history_file() -> None:
    """The payload names its schema, both versions and all history files."""
    payload = _payload()

    assert payload["schema"] == BACKUP_SCHEMA
    assert payload["schema_version"] == BACKUP_SCHEMA_VERSION
    assert payload["hsem_version"] == "7.1.0"
    assert payload["created"] == CREATED.isoformat()
    assert set(payload["history"]) == set(HISTORY_FILES)
    assert payload["history"]["hsem_savings_history.json"] == {"actual_savings": 12.5}
    assert payload["history"]["hsem_daily_history.json"] is None
    assert payload["solar_corrector"] == {"schema_version": 3}
    assert payload["forecast_tracker"] is None
    assert payload["average_sensors"] is None
    assert payload["configuration"] == {
        "entry_version": 4,
        "entry_minor_version": 1,
        "data": {"device_name": "HSEM"},
        "options": {"hsem_read_only": True},
    }


def test_redact_configuration_removes_secrets_and_key_material() -> None:
    """Secrets, certificate paths and PEM blocks are redacted; entity IDs stay."""
    redacted = redact_configuration(
        {
            "hsem_read_only": True,
            "hsem_house_power": "sensor.house_power",
            "api_token": "tok-123",
            "hsem_ocpp_tls_server_cert_path": "/ssl/hsem/ocpp/server.crt",
            "hsem_ocpp_tls_server_key_path": "/ssl/hsem/ocpp/server.key",
            "hsem_ocpp_tls_client_ca_path": "/ssl/hsem/ocpp/ca.crt",
            "hsem_ocpp_cpid": "CP-1",
            "key_passphrase": "open-sesame",
            "basic_auth": "dXNlcjpwdw==",
            "login_credentials": "user:pw",
            "notes": PEM,
            "nested": {"private_blob": "x", "inner": [PEM, {"pem": PEM}, 3]},
        }
    )

    assert redacted == {
        "hsem_read_only": True,
        "hsem_house_power": "sensor.house_power",
        "api_token": REDACTED,
        "hsem_ocpp_tls_server_cert_path": REDACTED,
        "hsem_ocpp_tls_server_key_path": REDACTED,
        "hsem_ocpp_tls_client_ca_path": REDACTED,
        "hsem_ocpp_cpid": REDACTED,
        "key_passphrase": REDACTED,
        "basic_auth": REDACTED,
        "login_credentials": REDACTED,
        "notes": REDACTED,
        "nested": {
            "private_blob": REDACTED,
            "inner": [REDACTED, {"pem": REDACTED}, 3],
        },
    }


def test_restorable_options_skips_redacted_values() -> None:
    """A redacted option, at any depth, is not restored."""
    payload = _payload(
        configuration={
            "options": {
                "hsem_read_only": True,
                "hsem_house_power": REDACTED,
                "nested": {"a": [1, REDACTED]},
                "plain_list": [1, 2],
            }
        }
    )

    options, skipped = restorable_options(payload)

    assert options == {"hsem_read_only": True, "plain_list": [1, 2]}
    assert skipped == ["hsem_house_power", "nested"]


@pytest.mark.parametrize("configuration", [None, {"options": "nope"}, {}])
def test_restorable_options_without_options(configuration: Any) -> None:
    """A payload without an options dict restores nothing."""
    assert restorable_options(_payload(configuration=configuration)) == ({}, [])


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (REASON_MANUAL, "hsem_backup_20261120_143000.json"),
        (REASON_BEFORE_IMPORT, "hsem_backup_20261120_143000_before_import.json"),
        (REASON_DELETED, "hsem_backup_20261120_143000_deleted.json"),
    ],
)
def test_backup_file_name(reason: str, expected: str) -> None:
    """The reason becomes the file-name suffix."""
    assert backup_file_name(CREATED, reason) == expected


def test_write_and_read_backup_round_trip(tmp_path: Path) -> None:
    """A written backup reads back unchanged."""
    payload = _payload()

    path = write_backup_file(tmp_path, payload)

    assert path == tmp_path / "hsem_backups" / "hsem_backup_20261120_143000.json"
    assert read_backup_file(tmp_path, path.name, hsem_version="7.1.0") == payload


def test_write_backup_file_failure_raises(tmp_path: Path) -> None:
    """A failed write is reported."""
    with (
        patch.object(backup, "write_json_history_file", return_value=False),
        pytest.raises(BackupError, match="Could not write"),
    ):
        write_backup_file(tmp_path, _payload())


def test_read_backup_file_missing_raises(tmp_path: Path) -> None:
    """A missing file is rejected."""
    with pytest.raises(BackupError, match="Could not read"):
        read_backup_file(tmp_path, "nope.json", hsem_version="7.1.0")


@pytest.mark.parametrize(
    "filename",
    [
        "",
        "../secrets.json",
        "sub/file.json",
        "/etc/passwd",
        ".hidden.json",
        "a.txt",
        "a\x00.json",
        "a\n.json",
    ],
)
def test_resolve_backup_path_rejects_paths(tmp_path: Path, filename: str) -> None:
    """Only a plain .json file name inside the backup folder is accepted."""
    with pytest.raises(BackupError, match="not a backup file name"):
        resolve_backup_path(tmp_path, filename)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema": "battmind_backup"}, "unknown schema"),
        ({"schema_version": BACKUP_SCHEMA_VERSION + 1}, "not supported"),
        ({"schema_version": 0}, "not supported"),
        ({"schema_version": True}, "not supported"),
        ({"schema_version": "1"}, "not supported"),
        ({"hsem_version": "7.2.0"}, "newer"),
        ({"hsem_version": "not a version"}, "valid HSEM version"),
        ({"history": None}, "history"),
        ({"history": {"hsem_savings_history.json": [1]}}, "history"),
        ({"solar_corrector": [1]}, "solar_corrector"),
        ({"forecast_tracker": "x"}, "forecast_tracker"),
        ({"average_sensors": 3}, "average_sensors"),
    ],
)
def test_validate_rejects(overrides: dict[str, Any], message: str) -> None:
    """An unknown schema, a newer version or a malformed payload is rejected."""
    with pytest.raises(BackupError, match=message):
        validate_backup_payload(_payload(**overrides), hsem_version="7.1.0")


def test_validate_rejects_non_dict() -> None:
    """A payload that is not an object is rejected."""
    with pytest.raises(BackupError, match="unknown schema"):
        validate_backup_payload([1], hsem_version="7.1.0")


@pytest.mark.parametrize("installed", ["7.1.0", "8.0.0", "unknown"])
def test_validate_accepts_same_older_or_unknown_installed(installed: str) -> None:
    """A backup from the same or an older HSEM is accepted."""
    payload = _payload()
    assert validate_backup_payload(payload, hsem_version=installed) is payload


def test_history_files_round_trip(tmp_path: Path) -> None:
    """History files are written from a backup and read back; null is left alone."""
    storage = tmp_path / ".storage"
    storage.mkdir()
    (storage / "hsem_daily_history.json").write_text('{"days": [1]}', encoding="utf-8")

    written = write_history_files(storage, _payload())

    assert written == ["hsem_savings_history.json"]
    assert read_pending_import(storage) == {"solar_corrector": {"schema_version": 3}}
    remove_pending_import(storage)
    remove_pending_import(storage)
    assert read_pending_import(storage) is None
    assert sorted(path.name for path in storage.iterdir()) == [
        "hsem_daily_history.json",
        "hsem_savings_history.json",
    ]
    history = read_history_files(storage)
    assert history["hsem_savings_history.json"] == {"actual_savings": 12.5}
    assert history["hsem_daily_history.json"] == {"days": [1]}
    assert history["hsem_prediction_history.json"] is None


def _two_file_payload() -> dict[str, Any]:
    """Return a payload that replaces two history files and no learner."""
    return _payload(
        history={
            "hsem_financial_history.json": {"import_cost": 9.0},
            "hsem_savings_history.json": {"actual_savings": 12.5},
        },
        solar_corrector=None,
    )


def test_write_history_files_staging_failure_changes_nothing(tmp_path: Path) -> None:
    """When a file cannot be staged, no target is touched."""
    (tmp_path / "hsem_financial_history.json").write_text("{}", encoding="utf-8")
    real_write = backup.write_json_history_file

    def _fail_second(path: Path, data: Any, **kwargs: Any) -> bool:
        return "savings" not in path.name and real_write(path, data, **kwargs)

    with (
        patch.object(backup, "write_json_history_file", side_effect=_fail_second),
        pytest.raises(BackupError, match="Could not write"),
    ):
        write_history_files(tmp_path, _two_file_payload())

    assert [path.name for path in tmp_path.iterdir()] == ["hsem_financial_history.json"]
    assert (tmp_path / "hsem_financial_history.json").read_text() == "{}"


def _fail_replace_number(number: int) -> Any:
    """Return an ``os.replace`` that raises on its *number*-th call."""
    calls = {"count": 0}
    real_replace = os.replace

    def _replace(src: Any, dst: Any) -> None:
        calls["count"] += 1
        if calls["count"] == number:
            raise OSError("disk went away")
        real_replace(src, dst)

    return _replace


def test_write_history_files_replace_failure_rolls_back(tmp_path: Path) -> None:
    """When the second file cannot be moved in, the first one is put back."""
    (tmp_path / "hsem_financial_history.json").write_text(
        '{"import_cost":1.0}', encoding="utf-8"
    )
    (tmp_path / "hsem_savings_history.json").write_text(
        '{"actual_savings":1.0}', encoding="utf-8"
    )

    # Calls 1 and 2 stage the two new files, 3 and 4 swap the financial file,
    # 5 moves the old savings file aside and 6, moving the new one in, fails.
    with (
        patch.object(backup.os, "replace", side_effect=_fail_replace_number(6)),
        pytest.raises(BackupError, match="Nothing was imported"),
    ):
        write_history_files(tmp_path, _two_file_payload())

    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "hsem_financial_history.json",
        "hsem_savings_history.json",
    ]
    assert json.loads((tmp_path / "hsem_financial_history.json").read_text()) == {
        "import_cost": 1.0
    }
    assert json.loads((tmp_path / "hsem_savings_history.json").read_text()) == {
        "actual_savings": 1.0
    }


def test_write_history_files_rollback_removes_a_new_file(tmp_path: Path) -> None:
    """A target that did not exist before is removed again on rollback."""
    # Calls 1 and 2 stage the two new files, 3 moves the new financial file
    # in and 4, for savings, fails.
    with (
        patch.object(backup.os, "replace", side_effect=_fail_replace_number(4)),
        pytest.raises(BackupError, match="Nothing was imported"),
    ):
        write_history_files(tmp_path, _two_file_payload())

    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("version", [3, None, True, "4"])
def test_check_configuration_version_rejects(version: Any) -> None:
    """Options from another entry version, or none, are rejected."""
    payload = _payload()
    payload["configuration"]["entry_version"] = version
    with pytest.raises(BackupError, match="config entry version"):
        check_configuration_version(payload, 4)


def test_check_configuration_version() -> None:
    """The same entry version passes; a payload without configuration does not."""
    check_configuration_version(_payload(), 4)
    with pytest.raises(BackupError, match="config entry version"):
        check_configuration_version(_payload(configuration=None), 4)


def test_debug_dump_redaction_keeps_certificate_facts() -> None:
    """Secrets go; the certificate facts of the OCPP TLS section stay."""
    dump = {
        "ocpp_tls": {"server_cert_expires": "2027-01-01", "ca_fingerprint": "AB:CD"},
        "config": {"hsem_ocpp_cpid": "CP-1", "key_passphrase": "x", "list": [PEM, 1]},
    }

    assert redact_debug_dump(dump) == {
        "ocpp_tls": {"server_cert_expires": "2027-01-01", "ca_fingerprint": "AB:CD"},
        "config": {
            "hsem_ocpp_cpid": REDACTED,
            "key_passphrase": REDACTED,
            "list": [REDACTED, 1],
        },
    }


def test_write_debug_file(tmp_path: Path) -> None:
    """The debug dump lands in hsem_debug."""
    path = write_debug_file(tmp_path, {"hsem_version": "7.1.0"}, CREATED)

    assert path == tmp_path / "hsem_debug" / "hsem_debug_20261120_143000.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"hsem_version": "7.1.0"}


def test_write_debug_file_failure_raises(tmp_path: Path) -> None:
    """A failed debug write is reported."""
    with (
        patch.object(backup, "write_json_history_file", return_value=False),
        pytest.raises(BackupError, match="debug file"),
    ):
        write_debug_file(tmp_path, {}, CREATED)


def test_issue_url_holds_versions_only() -> None:
    """The prefilled issue link carries the two versions and no user data."""
    url = build_issue_url("7.1.0", "2026.11.2")

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == (
        "https://github.com/woopstar/hsem/issues/new"
    )
    query = parse_qs(parts.query)
    assert set(query) == {"labels", "title", "body"}
    assert query["labels"] == ["bug"]
    body = query["body"][0]
    assert "- HSEM: 7.1.0" in body
    assert "- Home Assistant: 2026.11.2" in body
    assert "sensor." not in body
