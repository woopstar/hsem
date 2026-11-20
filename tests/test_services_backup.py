"""Tests for the backup, restore and debug-file services (issue #1436)."""

from __future__ import annotations

import json
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem import (
    async_remove_entry,
    services as services_module,
    services_backup,
)
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.models.savings_tracker import SavingsTracker
from custom_components.hsem.services import (
    SCHEMA_EXPORT_DIAGNOSTICS,
    SERVICE_HANDLER_MAP,
)
from custom_components.hsem.services_backup import (
    SCHEMA_IMPORT_DATA,
    SERVICE_EXPORT_DATA,
    SERVICE_IMPORT_DATA,
    async_handle_export_data,
    async_handle_import_data,
)
from custom_components.hsem.utils.backup import BackupError
from custom_components.hsem.utils.diagnostics import REDACTED
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

NOW = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 20, 14, 31, 0, tzinfo=UTC)

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG\n-----END PRIVATE KEY-----"


def _call(hass: HomeAssistant, data: dict[str, Any] | None = None) -> ServiceCall:
    """Return a ServiceCall carrying *data*."""
    return ServiceCall(hass=hass, domain=DOMAIN, service="test", data=data or {})


def _make_entry(*, corrector: SolarForecastCorrector | None = None) -> MagicMock:
    """Return a loaded HSEM config entry."""
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.state = ConfigEntryState.LOADED
    entry.data = {"device_name": "HSEM"}
    entry.options = {
        "hsem_read_only": True,
        "hsem_house_power": "sensor.house_power",
        "hsem_ocpp_tls_server_key_path": "/ssl/hsem/ocpp/server.key",
        "api_token": "tok-123",
    }
    entry.runtime_data = MagicMock()
    entry.runtime_data.coordinator._solar_corrector = corrector
    return entry


@pytest.fixture
def entry() -> MagicMock:
    """Return a loaded HSEM config entry with a solar corrector."""
    return _make_entry(corrector=SolarForecastCorrector())


@pytest.fixture
def hass(tmp_path: Path, entry: MagicMock) -> MagicMock:
    """Return a mocked Home Assistant whose config directory is ``tmp_path``."""
    hass = MagicMock(spec=HomeAssistant)
    hass.config = MagicMock()
    hass.config.config_dir = str(tmp_path)
    (tmp_path / ".storage").mkdir()

    async def _executor(func: Callable[..., Any], *args: Any) -> Any:
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_executor)
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_entries.return_value = [entry]

    async def _unload(_entry_id: str) -> bool:
        entry.state = ConfigEntryState.NOT_LOADED
        return True

    async def _setup(_entry_id: str) -> bool:
        entry.state = ConfigEntryState.LOADED
        return True

    def _update_entry(target: MagicMock, *, options: dict[str, Any]) -> None:
        target.options = options

    hass.config_entries.async_unload = AsyncMock(side_effect=_unload)
    hass.config_entries.async_setup = AsyncMock(side_effect=_setup)
    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    return hass


@pytest.fixture(autouse=True)
def fixed_environment() -> Generator[None]:
    """Pin the HSEM version and the clock."""
    version = AsyncMock(return_value="7.1.0")
    with (
        patch.object(services_backup, "async_get_hsem_integration_version", version),
        patch.object(services_module, "async_get_hsem_integration_version", version),
        patch.object(services_backup, "hsem_now", side_effect=[NOW, LATER, LATER]),
    ):
        yield


async def _save_savings(storage: Path, actual_savings: float) -> None:
    """Persist a savings history file through the real tracker."""
    tracker = SavingsTracker()
    tracker.history_file = str(storage / "hsem_savings_history.json")
    tracker.actual_savings = actual_savings
    assert await tracker.save_history()


async def _load_savings(storage: Path) -> SavingsTracker:
    """Load the savings history file through the real tracker."""
    tracker = SavingsTracker()
    tracker.history_file = str(storage / "hsem_savings_history.json")
    await tracker.load_history()
    return tracker


def test_services_are_registered() -> None:
    """Both services are in the handler map."""
    assert SERVICE_HANDLER_MAP[SERVICE_EXPORT_DATA][1] is async_handle_export_data
    assert SERVICE_HANDLER_MAP[SERVICE_IMPORT_DATA][1] is async_handle_import_data


def test_import_schema_defaults_restore_configuration_off() -> None:
    """restore_configuration is off unless asked for."""
    assert SCHEMA_IMPORT_DATA({"filename": "a.json"}) == {
        "filename": "a.json",
        "restore_configuration": False,
    }


@pytest.mark.asyncio
async def test_export_data_writes_one_versioned_file(
    hass: MagicMock, tmp_path: Path
) -> None:
    """export_data writes the history, the corrector and the redacted options."""
    await _save_savings(tmp_path / ".storage", 42.5)

    result = await async_handle_export_data(_call(hass))

    path = tmp_path / "hsem_backups" / "hsem_backup_20261120_143000.json"
    assert result == {"backup_path": str(path)}
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema"] == "hsem_backup"
    assert payload["schema_version"] == 1
    assert payload["hsem_version"] == "7.1.0"
    assert payload["reason"] == "manual"
    assert payload["history"]["hsem_savings_history.json"][
        "actual_savings"
    ] == pytest.approx(42.5)
    assert payload["history"]["hsem_daily_history.json"] is None
    assert payload["solar_corrector"]["schema_version"] >= 1
    assert payload["configuration"]["options"] == {
        "hsem_read_only": True,
        "hsem_house_power": REDACTED,
        "hsem_ocpp_tls_server_key_path": REDACTED,
        "api_token": REDACTED,
    }
    assert hass.async_add_executor_job.await_count == 2


@pytest.mark.asyncio
async def test_backup_never_holds_certificates_keys_or_tokens(
    hass: MagicMock, entry: MagicMock, tmp_path: Path
) -> None:
    """No OCPP certificate, key or token reaches the backup file."""
    ocpp_dir = tmp_path / "ssl" / "hsem" / "ocpp"
    ocpp_dir.mkdir(parents=True)
    (ocpp_dir / "ca.key").write_text(PEM, encoding="utf-8")
    (ocpp_dir / "server.crt").write_text(
        "-----BEGIN CERTIFICATE-----\nMIIDdzCCAl\n-----END CERTIFICATE-----",
        encoding="utf-8",
    )
    entry.data = {"password": "hunter2", "pasted_key": PEM}
    entry.options = {
        "api_token": "tok-123",
        "client_secret": "s3cret",
        "hsem_ocpp_tls_server_cert_path": str(ocpp_dir / "server.crt"),
        "hsem_ocpp_tls_server_key_path": str(ocpp_dir / "ca.key"),
        "hsem_ocpp_tls_client_ca_path": str(ocpp_dir / "ca.crt"),
        "nested": {"access_key": "AKIA123", "list": [PEM]},
    }

    result = await async_handle_export_data(_call(hass))

    raw = Path(result["backup_path"]).read_text(encoding="utf-8")
    for secret in (
        "BEGIN",
        "MIIEvQIBADANBgkqhkiG",
        "MIIDdzCCAl",
        "hunter2",
        "tok-123",
        "s3cret",
        "AKIA123",
        "server.crt",
        "ca.key",
        str(ocpp_dir),
    ):
        assert secret not in raw


@pytest.mark.asyncio
async def test_export_data_without_entry_raises(hass: MagicMock) -> None:
    """export_data needs a config entry."""
    hass.config_entries.async_entries.return_value = []

    with pytest.raises(ServiceValidationError, match="not configured"):
        await async_handle_export_data(_call(hass))


@pytest.mark.asyncio
async def test_export_data_write_failure_raises(hass: MagicMock) -> None:
    """A failed write surfaces as a HomeAssistantError."""
    with (
        patch.object(
            services_backup, "write_backup_file", side_effect=BackupError("disk full")
        ),
        pytest.raises(HomeAssistantError, match="disk full"),
    ):
        await async_handle_export_data(_call(hass))


@pytest.mark.asyncio
async def test_export_data_when_unloaded_has_no_corrector(
    hass: MagicMock, entry: MagicMock
) -> None:
    """An unloaded entry is exported without the solar corrector."""
    entry.runtime_data = None

    result = await async_handle_export_data(_call(hass))

    payload = json.loads(Path(result["backup_path"]).read_text(encoding="utf-8"))
    assert payload["solar_corrector"] is None


@pytest.mark.asyncio
async def test_import_data_replaces_history_and_reloads(
    hass: MagicMock, entry: MagicMock, tmp_path: Path
) -> None:
    """An import saves the current data, replaces it and reloads the entry."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    backup_name = Path(exported["backup_path"]).name
    await _save_savings(storage, 1.0)
    order: list[str] = []

    async def _unload(_entry_id: str) -> bool:
        order.append("unload")
        entry.state = ConfigEntryState.NOT_LOADED
        return True

    async def _setup(_entry_id: str) -> bool:
        # The reloaded tracker must find the imported file already in place.
        order.append(f"setup:{(await _load_savings(storage)).actual_savings}")
        entry.state = ConfigEntryState.LOADED
        return True

    hass.config_entries.async_unload.side_effect = _unload
    hass.config_entries.async_setup.side_effect = _setup

    result = await async_handle_import_data(
        _call(hass, SCHEMA_IMPORT_DATA({"filename": backup_name}))
    )

    before = (
        tmp_path / "hsem_backups" / "hsem_backup_20261120_143100_before_import.json"
    )
    assert order == ["unload", "setup:42.5"]
    assert result == {
        "imported_from": backup_name,
        "before_import_path": str(before),
        "history_files_restored": ["hsem_savings_history.json"],
        "solar_corrector_restored": True,
        "configuration_restored": False,
        "configuration_keys_skipped": [],
    }
    before_payload = json.loads(before.read_text(encoding="utf-8"))
    assert before_payload["reason"] == "before_import"
    assert before_payload["history"]["hsem_savings_history.json"][
        "actual_savings"
    ] == pytest.approx(1.0)
    # The reloaded tracker reads the imported file.
    assert (await _load_savings(storage)).actual_savings == pytest.approx(42.5)
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_import_data_restores_solar_corrector(
    hass: MagicMock, entry: MagicMock
) -> None:
    """The solar correction in the backup is loaded into the running corrector."""
    corrector = entry.runtime_data.coordinator._solar_corrector
    corrector.confidence = 0.8
    exported = await async_handle_export_data(_call(hass))
    corrector.confidence = 0.2

    await async_handle_import_data(
        _call(
            hass,
            SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name}),
        )
    )

    assert corrector.confidence == pytest.approx(0.8)


@pytest.mark.asyncio
async def test_import_data_restore_configuration_skips_redacted(
    hass: MagicMock, entry: MagicMock
) -> None:
    """restore_configuration merges plain options and keeps redacted ones."""
    exported = await async_handle_export_data(_call(hass))
    entry.options = {
        "hsem_read_only": False,
        "hsem_house_power": "sensor.new_house_power",
        "hsem_ocpp_tls_server_key_path": "/new/server.key",
        "api_token": "tok-456",
        "added_later": 7,
    }

    result = await async_handle_import_data(
        _call(
            hass,
            SCHEMA_IMPORT_DATA(
                {
                    "filename": Path(exported["backup_path"]).name,
                    "restore_configuration": True,
                }
            ),
        )
    )

    assert result["configuration_restored"] is True
    assert result["configuration_keys_skipped"] == [
        "api_token",
        "hsem_house_power",
        "hsem_ocpp_tls_server_key_path",
    ]
    assert entry.options == {
        "hsem_read_only": True,
        "hsem_house_power": "sensor.new_house_power",
        "hsem_ocpp_tls_server_key_path": "/new/server.key",
        "api_token": "tok-456",
        "added_later": 7,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"schema": "something_else"}, "unknown schema"),
        ({"schema_version": 99}, "not supported"),
        ({"hsem_version": "99.0.0"}, "newer"),
    ],
)
async def test_import_data_rejects_and_changes_nothing(
    hass: MagicMock,
    tmp_path: Path,
    overrides: dict[str, Any],
    message: str,
) -> None:
    """An unknown schema or a newer version is rejected before anything changes."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 1.0)
    exported = await async_handle_export_data(_call(hass))
    path = Path(exported["backup_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["history"]["hsem_savings_history.json"]["actual_savings"] = 99.0
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ServiceValidationError, match=message):
        await async_handle_import_data(
            _call(hass, SCHEMA_IMPORT_DATA({"filename": path.name}))
        )

    assert (await _load_savings(storage)).actual_savings == pytest.approx(1.0)
    assert [p.name for p in (tmp_path / "hsem_backups").iterdir()] == [path.name]
    hass.config_entries.async_unload.assert_not_called()
    hass.config_entries.async_setup.assert_not_called()
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
async def test_import_data_before_import_failure_changes_nothing(
    hass: MagicMock, tmp_path: Path
) -> None:
    """Without a before-import copy nothing is imported."""
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(tmp_path / ".storage", 1.0)

    with (
        patch.object(
            services_backup, "write_backup_file", side_effect=BackupError("disk full")
        ),
        pytest.raises(HomeAssistantError, match="Nothing was imported"),
    ):
        await async_handle_import_data(
            _call(
                hass,
                SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name}),
            )
        )

    hass.config_entries.async_unload.assert_not_called()
    assert (await _load_savings(tmp_path / ".storage")).actual_savings == pytest.approx(
        1.0
    )


@pytest.mark.asyncio
async def test_import_data_unload_failure_changes_nothing(
    hass: MagicMock, tmp_path: Path
) -> None:
    """When the entry cannot be unloaded nothing is replaced."""
    await _save_savings(tmp_path / ".storage", 42.5)
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(tmp_path / ".storage", 1.0)
    hass.config_entries.async_unload = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="could not be unloaded"):
        await async_handle_import_data(
            _call(
                hass,
                SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name}),
            )
        )

    hass.config_entries.async_setup.assert_not_called()
    assert (await _load_savings(tmp_path / ".storage")).actual_savings == pytest.approx(
        1.0
    )


@pytest.mark.asyncio
async def test_import_data_write_failure_sets_entry_up_again(
    hass: MagicMock,
) -> None:
    """A failed history write still starts HSEM again."""
    exported = await async_handle_export_data(_call(hass))

    with (
        patch.object(
            services_backup, "write_history_files", side_effect=BackupError("disk full")
        ),
        pytest.raises(HomeAssistantError, match="before the import"),
    ):
        await async_handle_import_data(
            _call(
                hass,
                SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name}),
            )
        )

    hass.config_entries.async_setup.assert_awaited_once_with("entry-1")


@pytest.mark.asyncio
async def test_import_data_reports_failed_restart(
    hass: MagicMock, entry: MagicMock
) -> None:
    """An entry that does not load after the import is reported."""
    exported = await async_handle_export_data(_call(hass))
    hass.config_entries.async_setup = AsyncMock(return_value=False)

    with pytest.raises(HomeAssistantError, match="did not start again"):
        await async_handle_import_data(
            _call(
                hass,
                SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name}),
            )
        )


@pytest.mark.asyncio
async def test_import_data_into_unloaded_entry(
    hass: MagicMock, entry: MagicMock
) -> None:
    """An entry that is not loaded is imported into without an unload."""
    exported = await async_handle_export_data(_call(hass))
    entry.state = ConfigEntryState.NOT_LOADED
    entry.runtime_data = None

    result = await async_handle_import_data(
        _call(
            hass,
            SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name}),
        )
    )

    hass.config_entries.async_unload.assert_not_called()
    hass.config_entries.async_setup.assert_awaited_once_with("entry-1")
    assert result["solar_corrector_restored"] is False


@pytest.mark.asyncio
async def test_remove_entry_writes_deleted_backup(
    hass: MagicMock, entry: MagicMock, tmp_path: Path
) -> None:
    """Deleting the entry writes a ..._deleted.json backup."""
    await _save_savings(tmp_path / ".storage", 42.5)
    entry.runtime_data = None

    await async_remove_entry(hass, entry)

    path = tmp_path / "hsem_backups" / "hsem_backup_20261120_143000_deleted.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["reason"] == "deleted"
    assert payload["history"]["hsem_savings_history.json"][
        "actual_savings"
    ] == pytest.approx(42.5)
    # The history files stay where they are.
    assert (tmp_path / ".storage" / "hsem_savings_history.json").exists()


@pytest.mark.asyncio
async def test_remove_entry_never_raises(hass: MagicMock, entry: MagicMock) -> None:
    """A failed backup does not block the removal."""
    with patch.object(
        services_backup, "write_backup_file", side_effect=OSError("read-only")
    ):
        await async_remove_entry(hass, entry)


def _diagnostics_coordinator() -> MagicMock:
    """Return a coordinator with a real planner input and output."""
    coordinator = MagicMock()
    coordinator.data = None
    planner_input = PlannerInput(previous_winner_name="sensor.house_power")
    planner_input.api_token = "tok-123"  # type: ignore[attr-defined]
    coordinator._last_planner_input = planner_input
    coordinator._last_planner_output = PlannerOutput()
    coordinator._ocpp_tls_status = {"mode": "tls", "ca_fingerprint": "AB:CD"}
    return coordinator


def test_export_diagnostics_schema_defaults_write_file_off() -> None:
    """write_file is off unless asked for."""
    assert SCHEMA_EXPORT_DIAGNOSTICS({}) == {"write_file": False}


@pytest.mark.asyncio
async def test_export_diagnostics_write_file(hass: MagicMock, tmp_path: Path) -> None:
    """write_file stores the redacted dump and notifies with a versions-only link."""
    with patch.object(
        services_module, "_get_coordinator", return_value=_diagnostics_coordinator()
    ):
        result = await services_module.async_handle_export_diagnostics(
            _call(hass, SCHEMA_EXPORT_DIAGNOSTICS({"write_file": True}))
        )

    path = tmp_path / "hsem_debug" / "hsem_debug_20261120_143000.json"
    assert result["debug_file"] == str(path)
    raw = path.read_text(encoding="utf-8")
    written = json.loads(raw)
    assert written == {k: v for k, v in result.items() if k != "debug_file"}
    assert written["hsem_version"] == "7.1.0"
    assert "sensor.house_power" not in raw
    assert "tok-123" not in raw

    hass.services.async_call.assert_awaited_once()
    domain, service, data = hass.services.async_call.await_args.args
    assert (domain, service) == ("persistent_notification", "create")
    assert str(path) in data["message"]
    url = data["message"].split("](")[1].split(")")[0]
    assert url.startswith("https://github.com/woopstar/hsem/issues/new?")
    query = parse_qs(urlsplit(url).query)
    assert set(query) == {"labels", "title", "body"}
    assert "- HSEM: 7.1.0" in query["body"][0]
    assert f"- Home Assistant: {HA_VERSION}" in query["body"][0]
    assert "sensor." not in url
    assert str(tmp_path) not in url


@pytest.mark.asyncio
async def test_export_diagnostics_without_write_file_writes_nothing(
    hass: MagicMock, tmp_path: Path
) -> None:
    """The default call writes no file and creates no notification."""
    with patch.object(
        services_module, "_get_coordinator", return_value=_diagnostics_coordinator()
    ):
        result = await services_module.async_handle_export_diagnostics(
            _call(hass, SCHEMA_EXPORT_DIAGNOSTICS({}))
        )

    assert "debug_file" not in result
    assert not (tmp_path / "hsem_debug").exists()
    hass.services.async_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_debug_export_write_failure_raises(hass: MagicMock) -> None:
    """A failed debug write surfaces as a HomeAssistantError."""
    with (
        patch.object(
            services_backup, "write_debug_file", side_effect=BackupError("disk full")
        ),
        pytest.raises(HomeAssistantError, match="disk full"),
    ):
        await services_backup.async_write_debug_export(hass, {}, "7.1.0")


@pytest.mark.asyncio
async def test_debug_export_survives_notification_failure(
    hass: MagicMock, tmp_path: Path
) -> None:
    """The file path is returned even when the notification cannot be created."""
    hass.services.async_call = AsyncMock(side_effect=HomeAssistantError("no service"))

    path = await services_backup.async_write_debug_export(hass, {"a": 1}, "7.1.0")

    assert Path(path).exists()
