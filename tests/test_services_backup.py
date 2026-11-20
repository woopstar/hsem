"""Tests for the backup, restore and debug-file services (issue #1436)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.entity_platform import DATA_ENTITY_PLATFORM
from homeassistant.helpers.restore_state import RestoreEntity

import custom_components.hsem as hsem_init
from custom_components.hsem import (
    HSEMRuntimeData,
    async_remove_entry,
    services as services_module,
    services_backup,
)
from custom_components.hsem.config_flow import HSEMConfigFlow
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator_lifecycle import CoordinatorLifecycleMixin
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
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
from custom_components.hsem.utils.backup import PENDING_IMPORT_FILE, BackupError
from custom_components.hsem.utils.diagnostics import REDACTED
from custom_components.hsem.utils.forecast_tracker import ForecastTracker
from custom_components.hsem.utils.solar_corrector import SolarForecastCorrector

NOW = datetime(2026, 11, 20, 14, 30, 0, tzinfo=UTC)
LATER = datetime(2026, 11, 20, 14, 31, 0, tzinfo=UTC)

PEM = "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG\n-----END PRIVATE KEY-----"


def _call(hass: HomeAssistant, data: dict[str, Any] | None = None) -> ServiceCall:
    """Return a ServiceCall carrying *data*."""
    return ServiceCall(hass=hass, domain=DOMAIN, service="test", data=data or {})


class _Coordinator:
    """A coordinator with the real update lock, hold and learners.

    The planning pipeline is left out; ``run_cycle`` stands in for a cycle a
    timer started: it holds the update lock and persists the savings tracker
    when it ends, as ``accumulate_meter_tracking`` does.
    """

    async_hold_updates = CoordinatorLifecycleMixin.async_hold_updates
    _async_wait_for_history_writers = (
        CoordinatorLifecycleMixin._async_wait_for_history_writers
    )

    def __init__(self, storage: Path, *, not_ready: bool = False) -> None:
        self._update_lock = asyncio.Lock()
        self._solar_corrector = SolarForecastCorrector()
        self._forecast_tracker = ForecastTracker(max_slots=2880)
        self._performance_task: asyncio.Task[None] | None = None
        self._daily_tracker = MagicMock(_midnight_task=None)
        self._savings_tracker = SavingsTracker()
        self._savings_tracker.history_file = str(storage / "hsem_savings_history.json")
        self._not_ready = not_ready
        self.listener_updates = 0

    async def async_setup(self) -> None:
        """Load the savings history, as the real setup does."""
        await self._savings_tracker.load_history()

    async def async_run_first_refresh(self) -> None:
        """Fail the way a source that is not ready yet does."""
        if self._not_ready:
            raise ConfigEntryNotReady("inverter not ready")

    async def async_teardown(self) -> None:
        """Nothing to cancel: this coordinator registers no timers."""

    def async_update_listeners(self) -> None:
        """Count listener updates."""
        self.listener_updates += 1

    async def run_cycle(self, gate: asyncio.Event) -> None:
        """Run one cycle that persists the savings tracker when it ends."""
        async with self._update_lock:
            await gate.wait()
            assert await self._savings_tracker.save_history()


class _Home:
    """Drives the real ``async_setup_entry`` / ``async_unload_entry``."""

    def __init__(self, hass: MagicMock, entry: MagicMock, storage: Path) -> None:
        self.hass = hass
        self.entry = entry
        self.storage = storage
        self.not_ready = False
        self.unload_ok = True
        self.events: list[str] = []

    def new_coordinator(self, *_args: Any) -> _Coordinator:
        """Build the coordinator ``async_setup_entry`` asks for."""
        return _Coordinator(self.storage, not_ready=self.not_ready)

    @property
    def coordinator(self) -> _Coordinator:
        """Return the running coordinator."""
        coordinator: _Coordinator = self.entry.runtime_data.coordinator
        return coordinator

    async def unload(self, _entry_id: str) -> bool:
        """Unload the entry the way Home Assistant does."""
        self.events.append("unload")
        self.hass.config_entries.async_unload_platforms.return_value = self.unload_ok
        ok = await hsem_init.async_unload_entry(self.hass, self.entry)
        self.entry.state = (
            ConfigEntryState.NOT_LOADED if ok else ConfigEntryState.FAILED_UNLOAD
        )
        return ok

    async def setup(self, _entry_id: str) -> bool:
        """Set the entry up the way Home Assistant does."""
        self.events.append("setup")
        try:
            await hsem_init.async_setup_entry(self.hass, self.entry)
        except ConfigEntryNotReady:
            self.entry.state = ConfigEntryState.SETUP_RETRY
            return False
        self.entry.state = ConfigEntryState.LOADED
        return True


@pytest.fixture
def entry() -> MagicMock:
    """Return an HSEM config entry."""
    entry = MagicMock()
    entry.entry_id = "entry-1"
    entry.version = HSEMConfigFlow.VERSION
    entry.minor_version = 1
    entry.state = ConfigEntryState.LOADED
    entry.data = {"device_name": "HSEM"}
    entry.options = {
        "hsem_read_only": True,
        "hsem_house_power": "sensor.house_power",
        "hsem_ocpp_tls_server_key_path": "/ssl/hsem/ocpp/server.key",
        "api_token": "tok-123",
    }
    return entry


@pytest.fixture
def hass(tmp_path: Path, entry: MagicMock) -> MagicMock:
    """Return a mocked Home Assistant whose config directory is ``tmp_path``."""
    hass = MagicMock(spec=HomeAssistant)
    hass.data = {}
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
    hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    def _update_entry(target: MagicMock, *, options: dict[str, Any]) -> None:
        target.options = options

    hass.config_entries.async_update_entry = MagicMock(side_effect=_update_entry)
    return hass


@pytest.fixture(autouse=True)
def home(hass: MagicMock, entry: MagicMock, tmp_path: Path) -> Generator[_Home]:
    """Return a loaded HSEM whose reload runs the real entry setup and unload."""
    home = _Home(hass, entry, tmp_path / ".storage")
    entry.runtime_data = HSEMRuntimeData(coordinator=home.new_coordinator())  # type: ignore[arg-type]
    hass.config_entries.async_unload = AsyncMock(side_effect=home.unload)
    hass.config_entries.async_setup = AsyncMock(side_effect=home.setup)
    with (
        patch.object(hsem_init, "HSEMDataUpdateCoordinator", home.new_coordinator),
        patch.object(
            hsem_init, "check_huawei_solar_version", AsyncMock(return_value=True)
        ),
        patch.object(hsem_init, "async_init_hsem_logger", AsyncMock()),
        patch.object(hsem_init, "async_close_hsem_logger", AsyncMock()),
        patch.object(hsem_init, "async_migrate_devices", AsyncMock()),
        patch.object(hsem_init, "_entity_gating_snapshot", return_value={}),
    ):
        yield home


@pytest.fixture(autouse=True)
def fixed_environment() -> Generator[None]:
    """Pin the HSEM version and the clock: NOW first, LATER from then on."""
    version = AsyncMock(return_value="7.1.0")
    clock = iter([NOW])
    with (
        patch.object(services_backup, "async_get_hsem_integration_version", version),
        patch.object(services_module, "async_get_hsem_integration_version", version),
        patch.object(
            services_backup, "hsem_now", side_effect=lambda: next(clock, LATER)
        ),
    ):
        yield


def _average_sensor(hass: MagicMock, measurements: dict[str, float]) -> HSEMAvgSensor:
    """Add a real hourly average sensor to HSEM's sensor platform."""
    sensor = HSEMAvgSensor(
        config_entry=MagicMock(entry_id="entry-1", options={}, data={}),
        hour_start=14,
        hour_end=15,
        avg=7,
        tracked_entity="sensor.utility",
        name="Average",
        unique_id="hsem_entry-1_average_14_15_7d",
        entity_id="sensor.hsem_average_14_15_7d",
    )
    sensor.hass = hass
    sensor._measurements = dict(measurements)
    # Home Assistant boundaries: the meter read, the listener and the state write.
    sensor._async_track_entities = AsyncMock()  # type: ignore[method-assign]
    sensor._async_store_utility_meter_value = AsyncMock()  # type: ignore[method-assign]
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]
    platform = MagicMock()
    platform.entities = {sensor.entity_id: sensor, "sensor.other": MagicMock()}
    hass.data[DATA_ENTITY_PLATFORM] = {DOMAIN: [platform]}
    return sensor


def _readd_on_setup(
    hass: MagicMock, sensor: HSEMAvgSensor, restored: dict[str, float]
) -> None:
    """Make the platform setup add *sensor* again, as a reload does.

    The sensor first restores *restored*, the samples it held before the
    unload, through its real ``async_added_to_hass``.
    """
    sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]
        return_value=SimpleNamespace(state="9.0", attributes={"measurements": restored})
    )
    sensor.async_get_last_extra_data = AsyncMock(return_value=None)  # type: ignore[method-assign]

    async def _forward(*_args: Any) -> bool:
        with (
            patch(
                "custom_components.hsem.custom_sensors.avg_sensor"
                ".async_track_time_interval",
                return_value=MagicMock(),
            ),
            patch.object(RestoreEntity, "async_added_to_hass", new_callable=AsyncMock),
        ):
            await sensor.async_added_to_hass()
        return True

    hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=_forward)


def _import_call(hass: MagicMock, exported: dict[str, Any], **data: Any) -> ServiceCall:
    """Return an import_data call for the file *exported* names."""
    return _call(
        hass,
        SCHEMA_IMPORT_DATA({"filename": Path(exported["backup_path"]).name, **data}),
    )


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
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """export_data writes the history, the learners and the redacted options."""
    await _save_savings(tmp_path / ".storage", 42.5)
    _average_sensor(hass, {"2026-11-19": 1.25})
    home.coordinator._forecast_tracker.get_or_create_record(
        datetime(2026, 11, 20, 14, 30, tzinfo=UTC),
        datetime(2026, 11, 20, 14, 45, tzinfo=UTC),
    )

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
    assert len(payload["forecast_tracker"]["records"]) == 1
    assert payload["average_sensors"] == {
        "14_15_7": {"measurements": {"2026-11-19": 1.25}}
    }
    assert payload["configuration"] == {
        "entry_version": HSEMConfigFlow.VERSION,
        "entry_minor_version": 1,
        "data": {"device_name": "HSEM"},
        "options": {
            "hsem_read_only": True,
            "hsem_house_power": "sensor.house_power",
            "hsem_ocpp_tls_server_key_path": REDACTED,
            "api_token": REDACTED,
        },
    }
    assert hass.async_add_executor_job.await_count == 2


@pytest.mark.asyncio
async def test_backup_never_holds_certificates_keys_or_tokens(
    hass: MagicMock, entry: MagicMock, tmp_path: Path
) -> None:
    """No certificate, key, token or charge point ID reaches the backup.

    Entity IDs are kept (owner decision), every secret is still redacted.
    """
    ocpp_dir = tmp_path / "ssl" / "hsem" / "ocpp"
    ocpp_dir.mkdir(parents=True)
    (ocpp_dir / "ca.key").write_text(PEM, encoding="utf-8")
    (ocpp_dir / "server.crt").write_text(
        "-----BEGIN CERTIFICATE-----\nMIIDdzCCAl\n-----END CERTIFICATE-----",
        encoding="utf-8",
    )
    entry.data = {"password": "hunter2", "pasted_key": PEM}
    entry.options = {
        "hsem_house_power": "sensor.house_power",
        "hsem_ev_charger_status": "sensor.wallbox_status",
        "api_token": "tok-123",
        "client_secret": "s3cret",
        "oauth_client_id": "client-9f2",
        "hsem_ocpp_cpid": "CP-GARAGE-01",
        "hsem_ocpp_second_cpid": "CP-DRIVEWAY-02",
        "key_passphrase": "open-sesame",
        "basic_auth": "dXNlcjpwdw==",
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
        "client-9f2",
        "CP-GARAGE-01",
        "CP-DRIVEWAY-02",
        "open-sesame",
        "dXNlcjpwdw==",
        "server.crt",
        "ca.key",
        str(ocpp_dir),
    ):
        assert secret not in raw
    # Entity IDs are kept, so the configuration can be restored elsewhere.
    options = json.loads(raw)["configuration"]["options"]
    assert options["hsem_house_power"] == "sensor.house_power"
    assert options["hsem_ev_charger_status"] == "sensor.wallbox_status"


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
async def test_export_data_when_never_loaded_has_no_learned_state(
    hass: MagicMock, entry: MagicMock
) -> None:
    """An entry that never ran is exported without learner state."""
    entry.runtime_data = None

    result = await async_handle_export_data(_call(hass))

    payload = json.loads(Path(result["backup_path"]).read_text(encoding="utf-8"))
    assert payload["solar_corrector"] is None
    assert payload["forecast_tracker"] is None
    assert payload["average_sensors"] is None


@pytest.mark.asyncio
async def test_import_data_replaces_history_and_reloads(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """An import saves the current data, replaces it and reloads the entry."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(storage, 1.0)

    result = await async_handle_import_data(_import_call(hass, exported))

    before = (
        tmp_path / "hsem_backups" / "hsem_backup_20261120_143100_before_import.json"
    )
    assert home.events == ["unload", "setup"]
    assert result == {
        "imported_from": Path(exported["backup_path"]).name,
        "before_import_path": str(before),
        "history_files_restored": ["hsem_savings_history.json"],
        "learned_state_restored": ["forecast_tracker", "solar_corrector"],
        "learned_state_pending": [],
        "configuration_restored": False,
        "configuration_keys_skipped": [],
    }
    before_payload = json.loads(before.read_text(encoding="utf-8"))
    assert before_payload["reason"] == "before_import"
    assert before_payload["history"]["hsem_savings_history.json"][
        "actual_savings"
    ] == pytest.approx(1.0)
    # The coordinator built by the real async_setup_entry loaded the import.
    assert home.coordinator._savings_tracker.actual_savings == pytest.approx(42.5)
    assert sorted(p.name for p in storage.iterdir()) == ["hsem_savings_history.json"]
    hass.config_entries.async_update_entry.assert_not_called()
    # The services the unload removed are registered again by the setup.
    assert hass.services.async_remove.call_count == len(SERVICE_HANDLER_MAP)


@pytest.mark.asyncio
async def test_import_waits_for_a_running_cycle(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """A cycle that is still running cannot write old data over the import.

    The teardown only cancels timers, so a cycle a timer already started
    persists its tracker when it ends. Without the hold on the update lock
    that save lands after the import and the old value wins.
    """
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    old_coordinator = home.coordinator
    old_coordinator._savings_tracker.actual_savings = 1.0
    gate = asyncio.Event()
    cycle = asyncio.create_task(old_coordinator.run_cycle(gate))
    await asyncio.sleep(0)
    assert old_coordinator._update_lock.locked()

    importing = asyncio.create_task(
        async_handle_import_data(_import_call(hass, exported))
    )
    for _ in range(20):
        await asyncio.sleep(0)

    # The import is parked behind the cycle and has changed nothing.
    assert not importing.done()
    assert home.events == []
    assert not (
        tmp_path / "hsem_backups" / "hsem_backup_20261120_143100_before_import.json"
    ).exists()

    gate.set()
    await cycle
    result = await importing

    # The before-import copy holds what the cycle saved; the import won.
    before = json.loads(Path(result["before_import_path"]).read_text(encoding="utf-8"))
    assert before["history"]["hsem_savings_history.json"][
        "actual_savings"
    ] == pytest.approx(1.0)
    assert (await _load_savings(storage)).actual_savings == pytest.approx(42.5)
    assert home.coordinator is not old_coordinator


@pytest.mark.asyncio
async def test_import_waits_for_background_writers(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """The hindsight evaluation and the midnight save finish before the import."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    coordinator = home.coordinator
    coordinator._savings_tracker.actual_savings = 1.0
    gate = asyncio.Event()

    async def _late_save() -> None:
        await gate.wait()
        assert await coordinator._savings_tracker.save_history()

    coordinator._performance_task = asyncio.create_task(_late_save())
    coordinator._daily_tracker._midnight_task = asyncio.create_task(_late_save())
    importing = asyncio.create_task(
        async_handle_import_data(_import_call(hass, exported))
    )
    for _ in range(20):
        await asyncio.sleep(0)
    assert not importing.done()
    assert home.events == []

    gate.set()
    await importing

    assert (await _load_savings(storage)).actual_savings == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_import_data_restores_learned_state(hass: MagicMock, home: _Home) -> None:
    """The learner state in the backup replaces what the new coordinator holds."""
    sensor = _average_sensor(hass, {"2026-11-19": 1.25, "2026-11-18": 0.75})
    home.coordinator._solar_corrector.confidence = 0.8
    slot = datetime(2026, 11, 20, 14, 30, tzinfo=UTC)
    home.coordinator._forecast_tracker.get_or_create_record(
        slot, datetime(2026, 11, 20, 14, 45, tzinfo=UTC)
    )
    exported = await async_handle_export_data(_call(hass))
    # The reload adds the sensor again; it restores what it held before.
    _readd_on_setup(hass, sensor, {"2026-11-20": 9.0})

    result = await async_handle_import_data(_import_call(hass, exported))

    assert result["learned_state_restored"] == [
        "average_sensors",
        "forecast_tracker",
        "solar_corrector",
    ]
    assert home.coordinator._solar_corrector.confidence == pytest.approx(0.8)
    assert home.coordinator._forecast_tracker.find_record(slot) is not None
    assert sensor.export_measurements() == pytest.approx(
        {"2026-11-19": 1.25, "2026-11-18": 0.75}
    )
    assert sensor.state == pytest.approx(1.0)
    # The sensors are told, so the loaded state reaches their restore state.
    assert home.coordinator.listener_updates == 1


@pytest.mark.asyncio
async def test_learned_state_survives_a_setup_that_is_retried(
    hass: MagicMock, home: _Home, entry: MagicMock, tmp_path: Path
) -> None:
    """The imported learner state is loaded by the setup that succeeds later."""
    pending = tmp_path / ".storage" / PENDING_IMPORT_FILE
    home.coordinator._solar_corrector.confidence = 0.8
    exported = await async_handle_export_data(_call(hass))
    home.not_ready = True

    with pytest.raises(HomeAssistantError, match="did not start again"):
        await async_handle_import_data(_import_call(hass, exported))

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert pending.exists()

    # Home Assistant retries the setup once the source is ready.
    home.not_ready = False
    assert await home.setup("entry-1")

    assert home.coordinator._solar_corrector.confidence == pytest.approx(0.8)
    assert not pending.exists()


@pytest.mark.asyncio
async def test_setup_without_pending_import_changes_nothing(
    hass: MagicMock, home: _Home
) -> None:
    """A normal start loads nothing and notifies no listener."""
    assert await home.setup("entry-1")

    assert home.coordinator._solar_corrector.confidence == pytest.approx(
        SolarForecastCorrector().confidence
    )
    assert home.coordinator.listener_updates == 0


@pytest.mark.asyncio
async def test_pending_import_with_bad_sections_does_not_block_setup(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """A learner section that cannot be loaded is skipped; the rest is loaded."""
    sensor = _average_sensor(hass, {})
    _readd_on_setup(hass, sensor, {"2026-11-19": 1.25})
    pending = tmp_path / ".storage" / PENDING_IMPORT_FILE
    pending.write_text(
        json.dumps(
            {
                "solar_corrector": {"schema_version": 3},
                "forecast_tracker": {"records": []},
                "average_sensors": {
                    "14_15_7": {"measurements": {"not-a-date": 1}},
                    "0_1_7": {"measurements": {}},
                },
            }
        ),
        encoding="utf-8",
    )

    with patch.object(
        ForecastTracker, "load_from_dict", side_effect=RuntimeError("boom")
    ):
        assert await home.setup("entry-1")

    assert hass.data["hsem_applied_import"] == ["solar_corrector"]
    # The sensor keeps the samples it restored.
    assert sensor.export_measurements() == {"2026-11-19": 1.25}
    assert not pending.exists()


@pytest.mark.asyncio
async def test_pending_import_without_learner_sections(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """A sensor the import does not know keeps its samples."""
    sensor = _average_sensor(hass, {})
    _readd_on_setup(hass, sensor, {"2026-11-19": 1.25})
    (tmp_path / ".storage" / PENDING_IMPORT_FILE).write_text(
        json.dumps({"average_sensors": {"0_1_7": {"measurements": {}}}}),
        encoding="utf-8",
    )

    assert await home.setup("entry-1")

    assert sensor.export_measurements() == {"2026-11-19": 1.25}
    assert hass.data["hsem_applied_import"] == []


@pytest.mark.asyncio
async def test_a_second_import_is_rejected_while_one_runs(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """Two imports never work on the same staged files or set up twice.

    The first import is parked behind a running cycle. A second call passed
    the entry-state check before the lock existed and then ran against the
    unloaded entry.
    """
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(storage, 1.0)
    gate = asyncio.Event()
    cycle = asyncio.create_task(home.coordinator.run_cycle(gate))
    await asyncio.sleep(0)
    first = asyncio.create_task(async_handle_import_data(_import_call(hass, exported)))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not first.done()

    with pytest.raises(ServiceValidationError, match="Another import is running"):
        await async_handle_import_data(_import_call(hass, exported))

    gate.set()
    await cycle
    await first

    assert home.events == ["unload", "setup"]
    assert len(list((tmp_path / "hsem_backups").iterdir())) == 2
    assert (await _load_savings(storage)).actual_savings == pytest.approx(42.5)
    # The lock is free again.
    await async_handle_import_data(_import_call(hass, exported))


@pytest.mark.asyncio
async def test_import_waits_for_a_midnight_save_that_starts_during_the_unload(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """A midnight save that starts while the entry unloads is over before the swap."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    coordinator = home.coordinator
    coordinator._savings_tracker.actual_savings = 1.0

    async def _late_save() -> None:
        for _ in range(10):
            await asyncio.sleep(0)
        assert await coordinator._savings_tracker.save_history()

    async def _teardown() -> None:
        # The midnight timer fires just before the teardown cancels it.
        coordinator._daily_tracker._midnight_task = asyncio.create_task(_late_save())

    coordinator.async_teardown = _teardown  # type: ignore[method-assign]

    await async_handle_import_data(_import_call(hass, exported))

    assert coordinator._daily_tracker._midnight_task.done()
    assert (await _load_savings(storage)).actual_savings == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_state_kept_from_an_unload_is_dropped_by_the_next_setup(
    hass: MagicMock, home: _Home
) -> None:
    """A later backup never pairs old learner state with newer history."""
    assert await home.unload("entry-1")
    assert "hsem_captured_learned_state" in hass.data

    assert await home.setup("entry-1")

    assert "hsem_captured_learned_state" not in hass.data


@pytest.mark.asyncio
async def test_import_data_restore_configuration_skips_redacted(
    hass: MagicMock, entry: MagicMock
) -> None:
    """restore_configuration restores entity IDs and keeps redacted options."""
    exported = await async_handle_export_data(_call(hass))
    entry.options = {
        "hsem_read_only": False,
        "hsem_house_power": "sensor.new_house_power",
        "hsem_ocpp_tls_server_key_path": "/new/server.key",
        "api_token": "tok-456",
        "added_later": 7,
    }

    result = await async_handle_import_data(
        _import_call(hass, exported, restore_configuration=True)
    )

    assert result["configuration_restored"] is True
    assert result["configuration_keys_skipped"] == [
        "api_token",
        "hsem_ocpp_tls_server_key_path",
    ]
    assert entry.options == {
        "hsem_read_only": True,
        "hsem_house_power": "sensor.house_power",
        "hsem_ocpp_tls_server_key_path": "/new/server.key",
        "api_token": "tok-456",
        "added_later": 7,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("backup_version", [HSEMConfigFlow.VERSION - 1, None])
async def test_restore_configuration_rejects_another_entry_version(
    hass: MagicMock,
    home: _Home,
    entry: MagicMock,
    tmp_path: Path,
    backup_version: int | None,
) -> None:
    """Options from another config entry version are not merged in."""
    await _save_savings(tmp_path / ".storage", 1.0)
    exported = await async_handle_export_data(_call(hass))
    path = Path(exported["backup_path"])
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["configuration"]["entry_version"] = backup_version
    path.write_text(json.dumps(payload), encoding="utf-8")
    options = dict(entry.options)

    with pytest.raises(ServiceValidationError, match="config entry version"):
        await async_handle_import_data(
            _import_call(hass, exported, restore_configuration=True)
        )

    assert home.events == []
    assert entry.options == options
    assert [p.name for p in (tmp_path / "hsem_backups").iterdir()] == [path.name]

    # The same file still restores the learned data.
    result = await async_handle_import_data(_import_call(hass, exported))
    assert result["configuration_restored"] is False


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
    home: _Home,
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
        await async_handle_import_data(_import_call(hass, exported))

    assert (await _load_savings(storage)).actual_savings == pytest.approx(1.0)
    assert [p.name for p in (tmp_path / "hsem_backups").iterdir()] == [path.name]
    assert home.events == []
    hass.config_entries.async_update_entry.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.SETUP_RETRY,
        ConfigEntryState.SETUP_ERROR,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.FAILED_UNLOAD,
        ConfigEntryState.MIGRATION_ERROR,
    ],
)
async def test_import_data_rejects_an_entry_that_cannot_be_reloaded(
    hass: MagicMock,
    home: _Home,
    entry: MagicMock,
    tmp_path: Path,
    state: ConfigEntryState,
) -> None:
    """An entry that is retrying, failed or half unloaded is left alone."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(storage, 1.0)
    entry.state = state

    with pytest.raises(ServiceValidationError, match="nothing was imported"):
        await async_handle_import_data(_import_call(hass, exported))

    assert home.events == []
    assert (await _load_savings(storage)).actual_savings == pytest.approx(1.0)
    assert len(list((tmp_path / "hsem_backups").iterdir())) == 1


@pytest.mark.asyncio
async def test_import_data_before_import_failure_changes_nothing(
    hass: MagicMock, home: _Home, tmp_path: Path
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
        await async_handle_import_data(_import_call(hass, exported))

    assert home.events == []
    assert (await _load_savings(tmp_path / ".storage")).actual_savings == pytest.approx(
        1.0
    )


@pytest.mark.asyncio
async def test_import_data_unload_failure_changes_nothing(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """When the entry cannot be unloaded nothing is replaced."""
    await _save_savings(tmp_path / ".storage", 42.5)
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(tmp_path / ".storage", 1.0)
    home.unload_ok = False

    with pytest.raises(HomeAssistantError, match="could not be unloaded"):
        await async_handle_import_data(_import_call(hass, exported))

    assert home.events == ["unload"]
    assert (await _load_savings(tmp_path / ".storage")).actual_savings == pytest.approx(
        1.0
    )


@pytest.mark.asyncio
async def test_import_data_failed_replace_rolls_back_and_restarts(
    hass: MagicMock, home: _Home, tmp_path: Path
) -> None:
    """When one file cannot be replaced, the files already replaced are put back."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    (storage / "hsem_daily_history.json").write_text('{"days": [1]}', encoding="utf-8")
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(storage, 1.0)
    (storage / "hsem_daily_history.json").write_text('{"days": [2]}', encoding="utf-8")
    # The last file to move in is the pending learner state. A non-empty
    # directory where its old copy must go makes that move fail, after the
    # savings and daily files were already replaced.
    (storage / PENDING_IMPORT_FILE).write_text("{}", encoding="utf-8")
    blocker = storage / f"{PENDING_IMPORT_FILE}.import_old"
    blocker.mkdir()
    (blocker / "keep").write_text("x", encoding="utf-8")

    with pytest.raises(HomeAssistantError, match="Nothing was imported"):
        await async_handle_import_data(_import_call(hass, exported))

    assert home.events == ["unload", "setup"]
    assert (await _load_savings(storage)).actual_savings == pytest.approx(1.0)
    assert json.loads((storage / "hsem_daily_history.json").read_text()) == {
        "days": [2]
    }
    # HSEM runs again on the data from before the import.
    assert home.coordinator._savings_tracker.actual_savings == pytest.approx(1.0)
    assert sorted(p.name for p in storage.iterdir()) == [
        "hsem_daily_history.json",
        blocker.name,
        "hsem_savings_history.json",
    ]


@pytest.mark.asyncio
async def test_import_data_into_unloaded_entry(
    hass: MagicMock, home: _Home, entry: MagicMock, tmp_path: Path
) -> None:
    """A disabled entry is imported into and left unloaded; learners wait."""
    storage = tmp_path / ".storage"
    await _save_savings(storage, 42.5)
    exported = await async_handle_export_data(_call(hass))
    await _save_savings(storage, 1.0)
    assert await home.unload("entry-1")
    home.events.clear()

    result = await async_handle_import_data(_import_call(hass, exported))

    assert home.events == []
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert result["learned_state_restored"] == []
    assert result["learned_state_pending"] == ["forecast_tracker", "solar_corrector"]
    assert (storage / PENDING_IMPORT_FILE).exists()
    # The state kept from the unload belonged to the replaced history.
    assert "hsem_captured_learned_state" not in hass.data
    assert (await _load_savings(storage)).actual_savings == pytest.approx(42.5)


@pytest.mark.asyncio
async def test_remove_entry_writes_deleted_backup_with_learned_state(
    hass: MagicMock, home: _Home, entry: MagicMock, tmp_path: Path
) -> None:
    """Deleting the entry writes a ..._deleted.json with the learner state.

    Home Assistant unloads the entry before it calls async_remove_entry, so
    the state has to be captured at the unload.
    """
    await _save_savings(tmp_path / ".storage", 42.5)
    _average_sensor(hass, {"2026-11-19": 1.25})
    home.coordinator._solar_corrector.confidence = 0.8

    assert await home.unload("entry-1")
    assert entry.runtime_data is None
    await async_remove_entry(hass, entry)

    path = tmp_path / "hsem_backups" / "hsem_backup_20261120_143100_deleted.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["reason"] == "deleted"
    assert payload["history"]["hsem_savings_history.json"][
        "actual_savings"
    ] == pytest.approx(42.5)
    assert payload["solar_corrector"]["confidence"] == pytest.approx(0.8)
    assert payload["forecast_tracker"] == {"records": []}
    assert payload["average_sensors"] == {
        "14_15_7": {"measurements": {"2026-11-19": 1.25}}
    }
    # The history files stay where they are.
    assert (tmp_path / ".storage" / "hsem_savings_history.json").exists()


@pytest.mark.asyncio
async def test_unload_goes_through_when_capture_fails(
    hass: MagicMock, home: _Home, entry: MagicMock
) -> None:
    """A learner that cannot be read does not block the unload."""
    with patch.object(
        SolarForecastCorrector, "to_dict", side_effect=RuntimeError("boom")
    ):
        assert await home.unload("entry-1")

    assert entry.runtime_data is None


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
    planner_input.ocpp_cpid = "CP-GARAGE-01"  # type: ignore[attr-defined]
    planner_input.key_passphrase = "open-sesame"  # type: ignore[attr-defined]
    planner_input.notes = PEM  # type: ignore[attr-defined]
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
    assert written["hsem_version"] == "7.1.0"
    assert written["planner_output"] == result["planner_output"]
    # The certificate facts of the OCPP TLS section stay readable.
    assert written["ocpp_tls"] == {"mode": "tls", "ca_fingerprint": "AB:CD"}
    for secret in (
        "sensor.house_power",
        "tok-123",
        "CP-GARAGE-01",
        "open-sesame",
        "BEGIN",
        "MIIEvQIBADANBgkqhkiG",
    ):
        assert secret not in raw

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
