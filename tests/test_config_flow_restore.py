"""Restore from a backup in the setup flow, on a real Home Assistant (issue #1448).

Every other backup test drives the services against a ``MagicMock`` Home
Assistant. Here a real Home Assistant core runs on a temporary config
directory, with its real config entries, flow manager, service registry and
``.storage`` folder: HSEM is set up as a config entry, exports a backup, is
removed, and is restored through the setup flow and through
``hsem.import_data``.

``pytest-homeassistant-custom-component`` cannot be installed next to the
pytest plugins this repository pins, so the instance is built the way
``tests/ml/test_statistics_reader_recorder.py`` builds its own.

Only the check for the Huawei Solar package is replaced: it is not installed
in the test environment. Nothing of the storage layer is mocked.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio

from homeassistant import bootstrap, loader
from homeassistant.config_entries import (
    SOURCE_USER,
    ConfigEntries,
    ConfigEntry,
    ConfigEntryState,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr

import custom_components.hsem as hsem_init
from custom_components.hsem import services_backup
from custom_components.hsem.config_flow import HSEMConfigFlow
from custom_components.hsem.const import DEFAULT_CONFIG_VALUES, DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.flows.restore import CONF_BACKUP_FILE
from custom_components.hsem.models.price_history_tracker import PriceHistoryTracker
from custom_components.hsem.services_backup import _IMPORT_LOCK_KEY
from custom_components.hsem.utils.backup import (
    BACKUP_DIR_NAME,
    HISTORY_FILES,
    PRICE_HISTORY_FILE,
    REASON_MANUAL,
    BackupError,
    build_backup_payload,
    find_backup_files,
    new_entry_configuration,
    write_backup_file,
)
from custom_components.hsem.utils.diagnostics import REDACTED
from custom_components.hsem.utils.schema import vol

# A forecast slot HSEM does not create itself: it is past the planning horizon.
SLOT = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
SLOT_END = SLOT + timedelta(minutes=15)
CREATED = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
VALID = "hsem_backup_20261120_143000.json"

#: Options a user changed after setup; they win over the entry data.
OPTIONS = {
    "hsem_update_interval": 9,
    "hsem_verbose_logging": True,
    "hsem_batteries_topup_min_power_pct": 25,
}

VERSION = HSEMConfigFlow.VERSION
MINOR_VERSION = HSEMConfigFlow.MINOR_VERSION


def _entry_data(**overrides: Any) -> dict[str, Any]:
    """Return the data of a complete HSEM entry: the defaults, read-only."""
    data = {
        key: value
        for key, value in DEFAULT_CONFIG_VALUES.items()
        if value is not vol.UNDEFINED
    }
    return {**data, "device_name": "HSEM test", "hsem_read_only": True, **overrides}


@pytest.fixture(autouse=True)
def huawei_solar_installed() -> Generator[None]:
    """Pass the check for the Huawei Solar package, which tests do not install."""
    with patch.object(hsem_init, "check_huawei_solar_version", return_value=True):
        yield


@pytest_asyncio.fixture
async def hass(tmp_path: Path) -> AsyncGenerator[HomeAssistant]:
    """A real Home Assistant core that can load HSEM from this repository."""
    instance = HomeAssistant(str(tmp_path))
    loader.async_setup(instance)
    instance.config_entries = ConfigEntries(instance, {})
    assert await bootstrap.async_load_base_functionality(instance)
    await instance.config_entries.async_initialize()
    # The loader scans <config>/custom_components; point it at this checkout.
    root = Path(hsem_init.__file__).parent
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    instance.data[loader.DATA_CUSTOM_COMPONENTS] = {
        DOMAIN: loader.Integration(
            instance,
            "custom_components.hsem",
            root,
            manifest,
            {path.name for path in root.iterdir()},
        )
    }
    instance.set_state(CoreState.running)
    try:
        yield instance
    finally:
        for entry in instance.config_entries.async_entries(DOMAIN):
            await instance.config_entries.async_unload(entry.entry_id)
        await instance.async_stop(force=True)


def _storage(hass: HomeAssistant) -> Path:
    """Return ``<config>/.storage``."""
    return Path(hass.config.config_dir) / ".storage"


def _history_on_disk(hass: HomeAssistant) -> list[str]:
    """Return the HSEM history files that exist."""
    return [name for name in HISTORY_FILES if (_storage(hass) / name).exists()]


def _coordinator(entry: ConfigEntry) -> HSEMDataUpdateCoordinator:
    """Return the coordinator of a loaded HSEM entry."""
    coordinator: HSEMDataUpdateCoordinator = entry.runtime_data.coordinator
    return coordinator


async def _add_entry(hass: HomeAssistant, **overrides: Any) -> ConfigEntry:
    """Set HSEM up as a config entry and return it, loaded."""
    entry = ConfigEntry(
        domain=DOMAIN,
        title="HSEM test",
        data=_entry_data(**overrides),
        options={},
        version=VERSION,
        minor_version=MINOR_VERSION,
        unique_id=DOMAIN,
        source=SOURCE_USER,
        discovery_keys={},  # type: ignore[arg-type]  # an empty mapping is what HA passes
        subentries_data=None,
    )
    await hass.config_entries.async_add(entry)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _learn(entry: ConfigEntry) -> None:
    """Give the running HSEM data to back up: a file and learner state."""
    coordinator = _coordinator(entry)
    coordinator._savings_tracker.actual_savings = 42.5
    assert await coordinator._savings_tracker.save_history()
    coordinator._solar_corrector.confidence = 0.8
    record = coordinator._forecast_tracker.get_or_create_record(SLOT, SLOT_END)
    record.forecast_pv_kwh = 1.5
    # Only a future slot with a raw forecast is kept in a backup.
    record.raw_forecast_pv_kwh = 1.5


def _assert_learned(entry: ConfigEntry) -> None:
    """Assert the running HSEM holds what :func:`_learn` gave it."""
    coordinator = _coordinator(entry)
    assert coordinator._savings_tracker.actual_savings == pytest.approx(42.5)
    assert coordinator._solar_corrector.confidence == pytest.approx(0.8)
    record = coordinator._forecast_tracker.find_record(SLOT)
    assert record is not None
    assert record.forecast_pv_kwh == pytest.approx(1.5)


async def _export(hass: HomeAssistant) -> str:
    """Call ``hsem.export_data`` and return the name of the written file."""
    response = await hass.services.async_call(
        DOMAIN, "export_data", {}, blocking=True, return_response=True
    )
    assert response is not None
    return Path(str(response["backup_path"])).name


def _write_backup(
    hass: HomeAssistant,
    *,
    created: datetime = CREATED,
    hsem_version: str = "1.0.0",
    entry_version: int = VERSION,
    data: dict[str, Any] | None = None,
    options: dict[str, Any] | None = None,
    history: dict[str, dict[str, Any] | None] | None = None,
) -> str:
    """Write a backup file by hand and return its name."""
    payload = build_backup_payload(
        hsem_version=hsem_version,
        created=created,
        reason=REASON_MANUAL,
        history=history or {},
        learned={},
        data=_entry_data() if data is None else data,
        options=options or {},
        entry_version=entry_version,
        entry_minor_version=MINOR_VERSION,
    )
    return write_backup_file(Path(hass.config.config_dir), payload).name


async def _start_flow(hass: HomeAssistant) -> dict[str, Any]:
    """Start the HSEM setup flow."""
    result: dict[str, Any] = await hass.config_entries.flow.async_init(  # type: ignore[assignment]  # a ConfigFlowResult is a dict
        DOMAIN, context={"source": SOURCE_USER}
    )
    return result


async def _configure(
    hass: HomeAssistant, result: dict[str, Any], user_input: dict[str, Any]
) -> dict[str, Any]:
    """Submit one step of a flow."""
    step: dict[str, Any] = await hass.config_entries.flow.async_configure(  # type: ignore[assignment]  # a ConfigFlowResult is a dict
        result["flow_id"], user_input
    )
    return step


async def _open_restore(hass: HomeAssistant) -> dict[str, Any]:
    """Start the flow and choose "restore" in the menu."""
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.MENU
    return await _configure(hass, result, {"next_step_id": "restore"})


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_export_remove_and_restore_through_the_setup_flow(
    hass: HomeAssistant,
) -> None:
    """A removed HSEM comes back from its backup with its data and options."""
    entry = await _add_entry(hass, hsem_update_interval=7)
    hass.config_entries.async_update_entry(entry, options=dict(OPTIONS))
    await hass.async_block_till_done()
    await _learn(entry)
    backup = await _export(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    # A new Home Assistant has no HSEM history files.
    for name in _history_on_disk(hass):
        (_storage(hass) / name).unlink()

    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "setup_method"
    assert result["menu_options"] == ["user", "restore"]

    result = await _configure(hass, result, {"next_step_id": "restore"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "restore"

    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "restore_confirm"
    placeholders = result["description_placeholders"]
    assert placeholders["backup_file"] == backup
    assert "hsem_ocpp_cpid" in placeholders["skipped_keys"]
    assert placeholders["disabled_features"] == "-"
    assert placeholders["missing_devices"] == "-"
    assert placeholders["history_files_skipped"] == "-"
    # Validating wrote nothing.
    assert _history_on_disk(hass) == []
    assert hass.config_entries.async_entries(DOMAIN) == []

    result = await _configure(hass, result, {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "HSEM test"

    (restored,) = hass.config_entries.async_entries(DOMAIN)
    assert restored.state is ConfigEntryState.LOADED
    assert restored.data["hsem_update_interval"] == 7
    assert restored.options == OPTIONS
    assert result["description"] is None
    assert REDACTED not in restored.data.values()
    assert "hsem_ocpp_cpid" not in restored.data
    _assert_learned(restored)


@pytest.mark.asyncio
async def test_export_and_import_through_the_service(hass: HomeAssistant) -> None:
    """``hsem.import_data`` puts the exported data back into a running HSEM."""
    entry = await _add_entry(hass)
    await _learn(entry)
    backup = await _export(hass)
    coordinator = _coordinator(entry)
    coordinator._savings_tracker.actual_savings = 1.0
    assert await coordinator._savings_tracker.save_history()
    coordinator._solar_corrector.confidence = 0.1
    coordinator._forecast_tracker.get_or_create_record(
        SLOT, SLOT_END
    ).forecast_pv_kwh = 9.0

    response = await hass.services.async_call(
        DOMAIN,
        "import_data",
        {"filename": backup, "restore_configuration": True},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()

    assert response is not None
    assert response["configuration_restored"] is True
    assert "hsem_savings_history.json" in response["history_files_restored"]  # type: ignore[operator]  # a list of file names
    assert entry.state is ConfigEntryState.LOADED
    _assert_learned(entry)


# ---------------------------------------------------------------------------
# When the restore step is offered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_setup_without_a_backup_starts_with_the_first_form(
    hass: HomeAssistant,
) -> None:
    """No backup folder: the flow is the one from before the restore step."""
    result = await _start_flow(hass)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


@pytest.mark.asyncio
async def test_a_backup_that_cannot_be_restored_still_offers_the_menu(
    hass: HomeAssistant,
) -> None:
    """Only a backup from a newer HSEM is there: the restore step says why."""
    too_new = _write_backup(hass, hsem_version="999.0.0")

    result = await _open_restore(hass)
    assert result["step_id"] == "restore"
    result = await _configure(hass, result, {CONF_BACKUP_FILE: too_new})

    assert result["errors"] == {"base": "backup_too_new"}


@pytest.mark.asyncio
async def test_menu_choice_set_up_from_scratch_runs_the_normal_flow(
    hass: HomeAssistant,
) -> None:
    """Choosing "user" in the menu shows the first form, which then advances."""
    _write_backup(hass)
    result = await _start_flow(hass)
    assert result["type"] is FlowResultType.MENU

    result = await _configure(hass, result, {"next_step_id": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await _configure(
        hass,
        result,
        {
            "device_name": "HSEM",
            "hsem_update_interval": 5,
            "hsem_read_only": True,
            "hsem_verbose_logging": False,
            "hsem_extended_attributes": False,
            "hsem_recommendation_interval_minutes": "60",
            "hsem_recommendation_interval_length": "24",
        },
    )
    assert result["step_id"] == "quick_setup"


@pytest.mark.asyncio
async def test_restore_form_lists_every_file_and_preselects_the_newest_valid(
    hass: HomeAssistant,
) -> None:
    """The form lists rejected files too, so choosing one explains why."""
    valid = _write_backup(hass)
    too_new = _write_backup(
        hass, created=datetime(2026, 11, 21, tzinfo=UTC), hsem_version="999.0.0"
    )

    result = await _open_restore(hass)

    schema = result["data_schema"].schema
    (field,) = schema
    assert field.default() == valid
    assert schema[field].config["options"] == [too_new, valid]


# ---------------------------------------------------------------------------
# Rejected backups: an error, and nothing written
# ---------------------------------------------------------------------------


def _reject_too_new(hass: HomeAssistant) -> str:
    return _write_backup(
        hass, created=datetime(2026, 11, 21, tzinfo=UTC), hsem_version="999.0.0"
    )


def _reject_other_entry_version(hass: HomeAssistant) -> str:
    return _write_backup(
        hass, created=datetime(2026, 11, 21, tzinfo=UTC), entry_version=VERSION - 1
    )


def _reject_not_json(hass: HomeAssistant) -> str:
    path = Path(hass.config.config_dir) / BACKUP_DIR_NAME / "broken.json"
    path.write_text("not json", encoding="utf-8")
    return path.name


def _reject_other_schema(hass: HomeAssistant) -> str:
    path = Path(hass.config.config_dir) / BACKUP_DIR_NAME / "other.json"
    path.write_text(json.dumps({"schema": "something_else"}), encoding="utf-8")
    return path.name


def _reject_without_configuration(hass: HomeAssistant) -> str:
    return _write_backup(hass, created=datetime(2026, 11, 21, tzinfo=UTC), data={})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write_rejected", "error"),
    [
        (_reject_too_new, "backup_too_new"),
        (_reject_other_entry_version, "backup_config_version"),
        (_reject_not_json, "backup_unreadable"),
        (_reject_other_schema, "backup_invalid"),
        (_reject_without_configuration, "backup_invalid"),
    ],
)
async def test_rejected_backup_shows_an_error_and_writes_nothing(
    hass: HomeAssistant, write_rejected: Any, error: str
) -> None:
    """A backup that fails a check keeps the user on the form."""
    _write_backup(hass, history={"hsem_savings_history.json": {"actual_savings": 1.0}})
    rejected = write_rejected(hass)
    result = await _open_restore(hass)

    result = await _configure(hass, result, {CONF_BACKUP_FILE: rejected})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "restore"
    assert result["errors"] == {"base": error}
    assert _history_on_disk(hass) == []
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_restore_aborts_when_the_backups_are_gone(hass: HomeAssistant) -> None:
    """The folder was emptied after the menu was shown."""
    name = _write_backup(hass)
    result = await _start_flow(hass)
    (Path(hass.config.config_dir) / BACKUP_DIR_NAME / name).unlink()

    result = await _configure(hass, result, {"next_step_id": "restore"})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_backups"


@pytest.mark.asyncio
async def test_restore_form_has_no_default_when_no_file_is_valid(
    hass: HomeAssistant,
) -> None:
    """The valid backup was removed after the menu; a rejected one is left."""
    name = _write_backup(hass)
    rejected = _reject_not_json(hass)
    result = await _start_flow(hass)
    (Path(hass.config.config_dir) / BACKUP_DIR_NAME / name).unlink()

    result = await _configure(hass, result, {"next_step_id": "restore"})

    (field,) = result["data_schema"].schema
    assert isinstance(field.default, vol.Undefined)
    assert result["data_schema"].schema[field].config["options"] == [rejected]


# ---------------------------------------------------------------------------
# The confirm step
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feature_that_needs_a_redacted_value_stays_off(
    hass: HomeAssistant,
) -> None:
    """OCPP was on in the backup; its charge point ID is not in the backup."""
    backup = _write_backup(
        hass, data=_entry_data(hsem_ocpp_enabled=True, hsem_ocpp_cpid="CP-1")
    )
    result = await _open_restore(hass)

    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})

    placeholders = result["description_placeholders"]
    assert "hsem_ocpp_cpid" in placeholders["skipped_keys"]
    assert placeholders["disabled_features"] == "hsem_ocpp_enabled"

    result = await _configure(hass, result, {})
    await hass.async_block_till_done()

    (restored,) = hass.config_entries.async_entries(DOMAIN)
    assert restored.options == {"hsem_ocpp_enabled": False}
    assert "CP-1" not in json.dumps(dict(restored.data))


@pytest.mark.asyncio
async def test_device_that_does_not_exist_here_is_left_empty(
    hass: HomeAssistant,
) -> None:
    """Device registry IDs of another Home Assistant are not restored."""
    other = ConfigEntry(
        domain="another_integration",
        title="Other",
        data={},
        options={},
        version=1,
        minor_version=1,
        unique_id="other",
        source=SOURCE_USER,
        discovery_keys={},  # type: ignore[arg-type]  # an empty mapping is what HA passes
        subentries_data=None,
    )
    hass.config_entries._entries[other.entry_id] = other
    known = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("another_integration", "1")}
    )
    backup = _write_backup(
        hass,
        data=_entry_data(
            hsem_huawei_solar_device_id_inverter_1="id-from-the-old-host",
            hsem_huawei_solar_device_id_inverter_2="",
            hsem_huawei_solar_device_id_batteries=known.id,
        ),
        options={"hsem_huawei_solar_device_id_batteries_2": "also-from-the-old-host"},
    )
    result = await _open_restore(hass)

    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})

    assert result["description_placeholders"]["missing_devices"] == (
        "hsem_huawei_solar_device_id_batteries_2, "
        "hsem_huawei_solar_device_id_inverter_1"
    )

    result = await _configure(hass, result, {})
    await hass.async_block_till_done()

    (restored,) = hass.config_entries.async_entries(DOMAIN)
    # The entry starts: a device is only needed when HSEM writes to it.
    assert restored.state is ConfigEntryState.LOADED
    assert restored.options == {
        "hsem_huawei_solar_device_id_batteries_2": "",
        "hsem_huawei_solar_device_id_inverter_1": "",
    }
    assert restored.data["hsem_huawei_solar_device_id_batteries"] == known.id


@pytest.mark.asyncio
async def test_history_on_disk_is_saved_before_it_is_replaced(
    hass: HomeAssistant,
) -> None:
    """History files survive a removal; a restore keeps a copy of them."""
    name = "hsem_savings_history.json"
    _storage(hass).mkdir(exist_ok=True)
    (_storage(hass) / name).write_text(json.dumps({"local": 1.0}), encoding="utf-8")
    backup = _write_backup(hass, history={name: {"actual_savings": 3.0}})
    result = await _open_restore(hass)
    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})

    result = await _configure(hass, result, {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["description"] == "history_saved"
    before = Path(result["description_placeholders"]["before_path"])
    assert before.name.endswith("_before_import.json")
    saved = json.loads(before.read_text(encoding="utf-8"))
    assert saved["history"][name] == {"local": 1.0}
    assert saved["configuration"]["data"] == {}


@pytest.mark.asyncio
async def test_restore_stops_when_the_history_on_disk_cannot_be_saved(
    hass: HomeAssistant,
) -> None:
    """No safety copy, no restore: the local history is left as it is."""
    name = "hsem_savings_history.json"
    _storage(hass).mkdir(exist_ok=True)
    (_storage(hass) / name).write_text(json.dumps({"local": 1.0}), encoding="utf-8")
    backup = _write_backup(hass, history={name: {"actual_savings": 3.0}})
    result = await _open_restore(hass)
    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})
    # A folder sits where the safety copy would be written.
    moment = datetime(2026, 11, 22, 8, 0, 0, tzinfo=UTC)
    blocker = (
        Path(hass.config.config_dir)
        / BACKUP_DIR_NAME
        / "hsem_backup_20261122_080000_before_import.json"
    )
    (blocker / "in_the_way").mkdir(parents=True)

    with patch.object(services_backup, "hsem_now", return_value=moment):
        result = await _configure(hass, result, {})

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "backup_safety_copy_failed"}
    assert json.loads((_storage(hass) / name).read_text()) == {"local": 1.0}
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_price_history_from_other_sensors_is_not_restored(
    hass: HomeAssistant,
) -> None:
    """The flow names the history file the import code path skips."""
    prices = PriceHistoryTracker()
    prices.set_source(
        {
            "import_entity": "sensor.another_import_price",
            "export_entity": "sensor.another_export_price",
            "import_unit": "",
            "export_unit": "",
        }
    )
    prices.record(SLOT, 15, 1.25, 0.5)
    backup = _write_backup(
        hass,
        history={
            PRICE_HISTORY_FILE: prices.as_dict(),
            "hsem_savings_history.json": {"actual_savings": 3.0},
        },
    )
    result = await _open_restore(hass)

    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})
    assert (
        result["description_placeholders"]["history_files_skipped"]
        == PRICE_HISTORY_FILE
    )
    result = await _configure(hass, result, {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    restored = json.loads((_storage(hass) / PRICE_HISTORY_FILE).read_text())
    assert restored != prices.as_dict()


@pytest.mark.asyncio
async def test_failed_write_shows_an_error_and_creates_nothing(
    hass: HomeAssistant,
) -> None:
    """The history files cannot be written: no entry, and the user can retry."""
    name = "hsem_savings_history.json"
    backup = _write_backup(hass, history={name: {"actual_savings": 3.0}})
    result = await _open_restore(hass)
    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})
    # A folder sits where the import stages the file.
    blocker = _storage(hass) / f"{name}.import_new"
    (blocker / "in_the_way").mkdir(parents=True)

    failed = await _configure(hass, result, {})

    assert failed["type"] is FlowResultType.FORM
    assert failed["step_id"] == "restore_confirm"
    assert failed["errors"] == {"base": "backup_write_failed"}
    assert _history_on_disk(hass) == []
    assert hass.config_entries.async_entries(DOMAIN) == []

    (blocker / "in_the_way").rmdir()
    blocker.rmdir()
    created = await _configure(hass, failed, {})
    await hass.async_block_till_done()
    assert created["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.asyncio
async def test_restore_waits_for_no_running_import(hass: HomeAssistant) -> None:
    """While ``hsem.import_data`` holds the import lock, nothing is written."""
    backup = _write_backup(
        hass, history={"hsem_savings_history.json": {"actual_savings": 3.0}}
    )
    result = await _open_restore(hass)
    result = await _configure(hass, result, {CONF_BACKUP_FILE: backup})
    lock = hass.data.setdefault(_IMPORT_LOCK_KEY, asyncio.Lock())

    async with lock:
        result = await _configure(hass, result, {})

    assert result["errors"] == {"base": "backup_write_failed"}
    assert _history_on_disk(hass) == []
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.asyncio
async def test_confirm_step_without_a_chosen_backup_shows_the_file_form(
    hass: HomeAssistant,
) -> None:
    """The confirm step never creates an entry without a validated backup."""
    _write_backup(hass)
    flow = HSEMConfigFlow()
    flow.hass = hass

    result = await flow.async_step_restore_confirm({})

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "restore"


# ---------------------------------------------------------------------------
# The configuration a backup gives a new entry
# ---------------------------------------------------------------------------


def _payload(data: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
    """Return a backup payload with *data* and *options*."""
    return build_backup_payload(
        hsem_version="1.0.0",
        created=CREATED,
        reason=REASON_MANUAL,
        history={},
        learned={},
        data=data,
        options=options,
        entry_version=VERSION,
        entry_minor_version=MINOR_VERSION,
    )


def test_ocpp_servers_stay_off_when_tls_is_on_and_the_paths_are_redacted() -> None:
    """Both servers are switched off rather than started without their TLS files."""
    configuration = new_entry_configuration(
        _payload(
            {
                "device_name": "HSEM",
                "hsem_ocpp_enabled": True,
                "hsem_ocpp_second_enabled": False,
                "hsem_ocpp_tls_server_cert_path": "/ssl/cert.pem",
            },
            {"hsem_ocpp_second_enabled": True, "hsem_ocpp_tls_mode": "tls"},
        )
    )

    assert configuration.disabled_features == [
        "hsem_ocpp_enabled",
        "hsem_ocpp_second_enabled",
    ]
    assert configuration.options == {
        "hsem_ocpp_enabled": False,
        "hsem_ocpp_second_enabled": False,
        "hsem_ocpp_tls_mode": "tls",
    }
    assert configuration.skipped_keys == ["hsem_ocpp_tls_server_cert_path"]
    assert configuration.data == {
        "device_name": "HSEM",
        "hsem_ocpp_enabled": True,
        "hsem_ocpp_second_enabled": False,
    }


def test_features_that_were_off_are_not_reported() -> None:
    """A redacted value only matters for a feature that was on."""
    configuration = new_entry_configuration(
        _payload({"device_name": "HSEM", "hsem_ocpp_cpid": "CP-1"}, {})
    )

    assert configuration.disabled_features == []
    assert configuration.options == {}
    assert configuration.skipped_keys == ["hsem_ocpp_cpid"]


def test_backup_error_keeps_its_code() -> None:
    """The code is the translation key the flow shows."""
    assert BackupError("x").code == "backup_invalid"
    assert BackupError("x", "backup_too_new").code == "backup_too_new"


def test_find_backup_files_ignores_other_files(tmp_path: Path) -> None:
    """Only ``.json`` files that are not hidden are checked."""
    folder = tmp_path / BACKUP_DIR_NAME
    folder.mkdir()
    (folder / "notes.txt").write_text("x", encoding="utf-8")
    (folder / ".hidden.json").write_text("{}", encoding="utf-8")
    (folder / "broken.json").write_text("{}", encoding="utf-8")

    found = find_backup_files(
        tmp_path, hsem_version="1.0.0", entry_version=4, entry_minor_version=1
    )

    assert found == {"broken.json": "backup_invalid"}
