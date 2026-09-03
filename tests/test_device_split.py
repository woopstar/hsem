"""Tests for the HSEM device split (issue #875).

Covers:
- ``devices.py`` — ``DeviceInfo`` construction for all 7 devices.
- ``HSEMEntity.device_info`` dispatch, including per-instance dynamic
  dispatch (EV primary/secondary, OCPP charger_index, switch/time/number
  description-driven devices).
- ``device_migration.py`` — offline ``unique_id`` classification and the
  one-time, idempotent entity-registry migration.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.hsem.const import DOMAIN, NAME
from custom_components.hsem.custom_numbers.battery_efficiency import (
    HSEMBatteryEfficiencyNumber,
)
from custom_components.hsem.custom_numbers.ev_target_soc import HSEMEVTargetSocNumber
from custom_components.hsem.custom_selectors.solcast_likelihood import (
    HSEMSolcastLikelihoodSelector,
)
from custom_components.hsem.custom_selectors.working_mode import HSEMWorkingModeSelector
from custom_components.hsem.custom_sensors.avg_sensor import HSEMAvgSensor
from custom_components.hsem.custom_sensors.battery_soc_sensor import (
    HSEMBatterySoCSensor,
)
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.ev_charger_calculated_power_sensor import (
    HSEMEVChargerCalculatedPowerSensor,
    HSEMEVSecondChargerCalculatedPowerSensor,
)
from custom_components.hsem.custom_sensors.financial_sensors import (
    HSEMExportIncomeSensor,
)
from custom_components.hsem.custom_sensors.forecast_accuracy_sensor import (
    HSEMForecastAccuracySensor,
)
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.ocpp_sensors import (
    HSEMOCPPChargerStatusSensor,
)
from custom_components.hsem.custom_switches.description import (
    HSEMSwitchEntityDescription,
)
from custom_components.hsem.custom_switches.switch import HSEMSwitch
from custom_components.hsem.custom_times.description import HSEMTimeEntityDescription
from custom_components.hsem.custom_times.time import HSEMTimeEntity
from custom_components.hsem.device_migration import (
    _ENTITY_ID_RENAMES,
    DEVICE_MIGRATION_DATA_KEY,
    DEVICE_MIGRATION_VERSION,
    async_migrate_devices,
    classify_entity_device,
)
from custom_components.hsem.devices import (
    HSEMDevice,
    get_device_identifier,
    get_device_info,
)
from custom_components.hsem.entity import HSEMEntity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ENTRY_ID = "test_entry_id"


def _mock_config_entry(**option_overrides: object) -> MagicMock:
    """Return a minimal mock ConfigEntry with real ``options``/``data`` dicts."""
    entry = MagicMock()
    entry.entry_id = _ENTRY_ID
    entry.options = dict(option_overrides)
    entry.data = {}
    return entry


def _mock_hass() -> MagicMock:
    """Return a minimal Home Assistant mock."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _mock_coordinator() -> MagicMock:
    """Return a minimal mock HSEMDataUpdateCoordinator."""
    coordinator = MagicMock()
    coordinator.data = None
    coordinator.last_update_success = True
    return coordinator


# ---------------------------------------------------------------------------
# devices.py
# ---------------------------------------------------------------------------


class TestGetDeviceIdentifier:
    """Controller keeps the pre-split identifier; every other device is new."""

    def test_controller_keeps_bare_entry_id(self) -> None:
        assert get_device_identifier(_ENTRY_ID, HSEMDevice.CONTROLLER) == _ENTRY_ID

    @pytest.mark.parametrize(
        "device",
        [
            HSEMDevice.BATTERY_ENERGY,
            HSEMDevice.HOURLY_CONSUMPTION,
            HSEMDevice.FINANCIAL,
            HSEMDevice.FORECAST,
            HSEMDevice.EV_PRIMARY,
            HSEMDevice.EV_SECONDARY,
        ],
    )
    def test_non_controller_devices_get_suffixed_identifier(
        self, device: HSEMDevice
    ) -> None:
        identifier = get_device_identifier(_ENTRY_ID, device)
        assert identifier == f"{_ENTRY_ID}_{device.value}"
        assert identifier != _ENTRY_ID


class TestGetDeviceInfo:
    """DeviceInfo construction for all 7 devices."""

    def test_all_devices_produce_distinct_device_info(self) -> None:
        infos = [get_device_info(_ENTRY_ID, device) for device in HSEMDevice]
        identifiers = [info["identifiers"] for info in infos]
        assert len(identifiers) == len(set(frozenset(i) for i in identifiers)), (
            "Every device must have a unique DeviceInfo identifier"
        )

    def test_controller_device_info_matches_legacy_shape(self) -> None:
        """Controller DeviceInfo is byte-for-byte the pre-split DeviceInfo."""
        info = get_device_info(_ENTRY_ID, HSEMDevice.CONTROLLER)
        assert info == DeviceInfo(
            identifiers={(DOMAIN, _ENTRY_ID)},
            name=NAME,
            manufacturer=DOMAIN.upper(),
            model="Custom Integration",
        )

    def test_manufacturer_and_model_consistent_across_devices(self) -> None:
        for device in HSEMDevice:
            info = get_device_info(_ENTRY_ID, device)
            assert info["manufacturer"] == DOMAIN.upper()
            assert info["model"] == "Custom Integration"


# ---------------------------------------------------------------------------
# HSEMEntity.device_info dispatch
# ---------------------------------------------------------------------------


class TestHSEMEntityDefault:
    """The base HSEMEntity defaults to the CONTROLLER device."""

    def test_default_device_is_controller(self) -> None:
        entity = HSEMEntity(_mock_config_entry())
        assert entity.device_info["identifiers"] == {(DOMAIN, _ENTRY_ID)}


class TestPerEntityDeviceInfo:
    """Each concrete entity class resolves device_info to its target device."""

    def _identifiers(self, entity: HSEMEntity) -> set[tuple[str, str]]:
        info = entity.device_info
        assert info is not None
        return set(info["identifiers"])

    def test_controller_entity(self) -> None:
        sensor = HSEMDegradedModeSensor(_mock_config_entry(), _mock_coordinator())
        assert self._identifiers(sensor) == {(DOMAIN, _ENTRY_ID)}

    def test_battery_energy_entity(self) -> None:
        sensor = HSEMBatterySoCSensor(_mock_config_entry(), _mock_coordinator())
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.BATTERY_ENERGY.value}")
        }

    def test_hourly_consumption_house_power_sensor(self) -> None:
        sensor = HSEMHouseConsumptionPowerSensor(
            _mock_config_entry(), 13, 14, MagicMock()
        )
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.HOURLY_CONSUMPTION.value}")
        }

    def test_hourly_consumption_avg_sensor(self) -> None:
        sensor = HSEMAvgSensor(
            config_entry=_mock_config_entry(),
            hour_start=13,
            hour_end=14,
            avg=7,
            tracked_entity="sensor.hsem_house_consumption_energy_13_14_utility_meter",
            name="13:00-14:00 7-Day Average",
            unique_id="hsem_test_entry_id_house_consumption_energy_avg_13_14_7d",
            entity_id="sensor.hsem_house_consumption_energy_avg_13_14_7d",
        )
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.HOURLY_CONSUMPTION.value}")
        }

    def test_financial_entity(self) -> None:
        sensor = HSEMExportIncomeSensor(_mock_config_entry(), _mock_coordinator())
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.FINANCIAL.value}")
        }

    def test_forecast_sensor_entity(self) -> None:
        sensor = HSEMForecastAccuracySensor(_mock_config_entry(), _mock_coordinator())
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.FORECAST.value}")
        }

    def test_forecast_select_entity(self) -> None:
        description = SelectEntityDescription(key="hsem_solcast_likelihood")
        selector = HSEMSolcastLikelihoodSelector(
            _mock_hass(), _mock_config_entry(), description
        )
        assert self._identifiers(selector) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.FORECAST.value}")
        }

    def test_ev_primary_calculated_power_sensor(self) -> None:
        sensor = HSEMEVChargerCalculatedPowerSensor(
            _mock_config_entry(), _mock_coordinator()
        )
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_PRIMARY.value}")
        }

    def test_ev_secondary_calculated_power_sensor(self) -> None:
        sensor = HSEMEVSecondChargerCalculatedPowerSensor(
            _mock_config_entry(), _mock_coordinator()
        )
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_SECONDARY.value}")
        }

    def test_ocpp_primary_charger_sensor(self) -> None:
        sensor = HSEMOCPPChargerStatusSensor(
            _mock_config_entry(), _mock_coordinator(), charger_index=1
        )
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_PRIMARY.value}")
        }

    def test_ocpp_secondary_charger_sensor(self) -> None:
        sensor = HSEMOCPPChargerStatusSensor(
            _mock_config_entry(), _mock_coordinator(), charger_index=2
        )
        assert self._identifiers(sensor) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_SECONDARY.value}")
        }

    def test_ev_target_soc_number_primary(self) -> None:
        description = NumberEntityDescription(key="hsem_ev_target_soc")
        number = HSEMEVTargetSocNumber(
            _mock_hass(),
            _mock_config_entry(),
            description,
            config_key="hsem_ev_target_soc",
            is_second=False,
        )
        assert self._identifiers(number) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_PRIMARY.value}")
        }

    def test_ev_target_soc_number_secondary(self) -> None:
        description = NumberEntityDescription(key="hsem_ev_second_target_soc")
        number = HSEMEVTargetSocNumber(
            _mock_hass(),
            _mock_config_entry(),
            description,
            config_key="hsem_ev_second_target_soc",
            is_second=True,
        )
        assert self._identifiers(number) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_SECONDARY.value}")
        }

    def test_battery_efficiency_number(self) -> None:
        description = NumberEntityDescription(key="hsem_charge_efficiency")
        number = HSEMBatteryEfficiencyNumber(
            _mock_hass(),
            _mock_config_entry(),
            description,
            config_key="hsem_batteries_charge_efficiency",
        )
        assert self._identifiers(number) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.BATTERY_ENERGY.value}")
        }

    def test_working_mode_selector_stays_on_controller(self) -> None:
        description = SelectEntityDescription(
            key="hsem_force_working_mode", options=["auto"]
        )
        selector = HSEMWorkingModeSelector(
            _mock_hass(), _mock_config_entry(), description, "auto"
        )
        assert self._identifiers(selector) == {(DOMAIN, _ENTRY_ID)}

    def test_switch_uses_description_hsem_device(self) -> None:
        description = HSEMSwitchEntityDescription(
            key="hsem_batteries_enable_batteries_schedule_1",
            hsem_device=HSEMDevice.BATTERY_ENERGY,
        )
        switch = HSEMSwitch(_mock_hass(), _mock_config_entry(), description)
        assert self._identifiers(switch) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.BATTERY_ENERGY.value}")
        }

    def test_switch_default_description_device_is_controller(self) -> None:
        description = HSEMSwitchEntityDescription(key="hsem_read_only")
        switch = HSEMSwitch(_mock_hass(), _mock_config_entry(), description)
        assert self._identifiers(switch) == {(DOMAIN, _ENTRY_ID)}

    def test_time_uses_description_hsem_device(self) -> None:
        description = HSEMTimeEntityDescription(
            key="hsem_ev_second_deadline_time",
            default_value="07:00:00",
            hsem_device=HSEMDevice.EV_SECONDARY,
        )
        time_entity = HSEMTimeEntity(_mock_hass(), _mock_config_entry(), description)
        assert self._identifiers(time_entity) == {
            (DOMAIN, f"{_ENTRY_ID}_{HSEMDevice.EV_SECONDARY.value}")
        }


# ---------------------------------------------------------------------------
# device_migration.py — classification
# ---------------------------------------------------------------------------


class TestClassifyEntityDevice:
    """Offline unique_id -> HSEMDevice classification used by the migration."""

    @pytest.mark.parametrize(
        ("unique_id", "expected"),
        [
            # Controller (default / unchanged entities).
            (f"{DOMAIN}_{_ENTRY_ID}_workingmode_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_degraded_mode_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_read_only_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_hardware_writes_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_missing_entities_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_force_mode_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_last_updated_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_next_update_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_update_interval_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_applier_status_sensor", HSEMDevice.CONTROLLER),
            (f"{DOMAIN}_{_ENTRY_ID}_plan_explanation_sensor", HSEMDevice.CONTROLLER),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_daily_plan_vs_actual_sensor",
                HSEMDevice.CONTROLLER,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_recommendation_interval_sensor",
                HSEMDevice.CONTROLLER,
            ),
            # Battery & Energy.
            (f"{DOMAIN}_{_ENTRY_ID}_battery_soc_sensor", HSEMDevice.BATTERY_ENERGY),
            (
                f"{DOMAIN}_{_ENTRY_ID}_effective_discharge_floor_sensor",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_net_consumption_sensor",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_pv_curtailment_sensor",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_battery_charge_efficiency",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_battery_discharge_efficiency",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_batteries_enable_batteries_schedule_1_switch",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_batteries_enable_batteries_schedule_2_start_time",
                HSEMDevice.BATTERY_ENERGY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_dynamic_discharge_floor_switch",
                HSEMDevice.BATTERY_ENERGY,
            ),
            # Hourly Consumption Profile.
            (
                f"{DOMAIN}_{_ENTRY_ID}_house_consumption_power_13_14",
                HSEMDevice.HOURLY_CONSUMPTION,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_house_consumption_energy_integral_13_14",
                HSEMDevice.HOURLY_CONSUMPTION,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_house_consumption_energy_avg_13_14_7d",
                HSEMDevice.HOURLY_CONSUMPTION,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_house_consumption_energy_13_14_utility_meter",
                HSEMDevice.HOURLY_CONSUMPTION,
            ),
            # Financial.
            (f"{DOMAIN}_{_ENTRY_ID}_export_income_sensor", HSEMDevice.FINANCIAL),
            (f"{DOMAIN}_{_ENTRY_ID}_import_cost_sensor", HSEMDevice.FINANCIAL),
            (f"{DOMAIN}_{_ENTRY_ID}_net_grid_balance_sensor", HSEMDevice.FINANCIAL),
            (f"{DOMAIN}_{_ENTRY_ID}_savings_tracker_sensor", HSEMDevice.FINANCIAL),
            # Forecast.
            (f"{DOMAIN}_{_ENTRY_ID}_forecast_accuracy_sensor", HSEMDevice.FORECAST),
            (f"{DOMAIN}_{_ENTRY_ID}_solar_confidence_sensor", HSEMDevice.FORECAST),
            (f"{DOMAIN}_{_ENTRY_ID}_prediction_accuracy_sensor", HSEMDevice.FORECAST),
            (f"{DOMAIN}_solcast_likelihood_{_ENTRY_ID}", HSEMDevice.FORECAST),
            # EV Primary.
            (f"{DOMAIN}_{_ENTRY_ID}_ev_charging_sensor", HSEMDevice.EV_PRIMARY),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ev_optimal_charging_plan",
                HSEMDevice.EV_PRIMARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ev_charger_calculated_power",
                HSEMDevice.EV_PRIMARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ev_charger_current_limit",
                HSEMDevice.EV_PRIMARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_ev_target_soc_number",
                HSEMDevice.EV_PRIMARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ocpp_charger_status_sensor",
                HSEMDevice.EV_PRIMARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ocpp_charger_sessions_sensor",
                HSEMDevice.EV_PRIMARY,
            ),
            # EV Secondary.
            (
                f"{DOMAIN}_{_ENTRY_ID}_ev_second_optimal_charging_plan",
                HSEMDevice.EV_SECONDARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ev_second_charger_current_limit",
                HSEMDevice.EV_SECONDARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ev_second_charger_calculated_power",
                HSEMDevice.EV_SECONDARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_ev_second_target_soc_number",
                HSEMDevice.EV_SECONDARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_{DOMAIN}_ev_second_deadline_time_time",
                HSEMDevice.EV_SECONDARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ocpp_charger_status_sensor_second",
                HSEMDevice.EV_SECONDARY,
            ),
            (
                f"{DOMAIN}_{_ENTRY_ID}_ocpp_charger_sessions_sensor_second",
                HSEMDevice.EV_SECONDARY,
            ),
        ],
    )
    def test_classification(self, unique_id: str, expected: HSEMDevice) -> None:
        assert classify_entity_device(unique_id) is expected


# ---------------------------------------------------------------------------
# device_migration.py — async_migrate_devices
# ---------------------------------------------------------------------------


def _make_entity_entry(
    entity_id: str, unique_id: str, device_id: str | None = None
) -> MagicMock:
    entry = MagicMock()
    entry.entity_id = entity_id
    entry.unique_id = unique_id
    entry.device_id = device_id
    return entry


class TestAsyncMigrateDevices:
    """One-time, idempotent entity-registry migration."""

    @pytest.mark.asyncio
    async def test_migrates_device_and_marks_entry(self) -> None:
        hass = _mock_hass()
        config_entry = _mock_config_entry()

        entity_entries = [
            _make_entity_entry(
                "sensor.hsem_battery_soc_sensor",
                f"{DOMAIN}_{_ENTRY_ID}_battery_soc_sensor",
                device_id="old-controller-device",
            ),
        ]

        entity_reg = MagicMock()
        device_reg = MagicMock()
        new_device = MagicMock()
        new_device.id = "device-battery-energy"
        device_reg.async_get_or_create.return_value = new_device

        with (
            patch(
                "custom_components.hsem.device_migration.er.async_get",
                return_value=entity_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.dr.async_get",
                return_value=device_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.er.async_entries_for_config_entry",
                return_value=entity_entries,
            ),
        ):
            await async_migrate_devices(hass, config_entry)

        entity_reg.async_update_entity.assert_called_once_with(
            "sensor.hsem_battery_soc_sensor", device_id="device-battery-energy"
        )
        hass.config_entries.async_update_entry.assert_called_once()
        _, kwargs = hass.config_entries.async_update_entry.call_args
        assert kwargs["data"][DEVICE_MIGRATION_DATA_KEY] == DEVICE_MIGRATION_VERSION
        # unique_id must never be touched by the migration.
        assert "new_unique_id" not in entity_reg.async_update_entity.call_args.kwargs

    @pytest.mark.asyncio
    async def test_entity_already_on_correct_device_is_untouched(self) -> None:
        hass = _mock_hass()
        config_entry = _mock_config_entry()

        entity_entries = [
            _make_entity_entry(
                "sensor.hsem_battery_soc_sensor",
                f"{DOMAIN}_{_ENTRY_ID}_battery_soc_sensor",
                device_id="device-battery-energy",
            ),
        ]

        entity_reg = MagicMock()
        device_reg = MagicMock()
        existing_device = MagicMock()
        existing_device.id = "device-battery-energy"
        device_reg.async_get_or_create.return_value = existing_device

        with (
            patch(
                "custom_components.hsem.device_migration.er.async_get",
                return_value=entity_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.dr.async_get",
                return_value=device_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.er.async_entries_for_config_entry",
                return_value=entity_entries,
            ),
        ):
            await async_migrate_devices(hass, config_entry)

        entity_reg.async_update_entity.assert_not_called()
        # The migration-version flag is still recorded even when no entity
        # needed a device move, so a second run is still a no-op.
        hass.config_entries.async_update_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_second_run_is_a_noop(self) -> None:
        hass = _mock_hass()
        config_entry = _mock_config_entry()
        config_entry.data = {DEVICE_MIGRATION_DATA_KEY: DEVICE_MIGRATION_VERSION}

        entity_reg = MagicMock()
        device_reg = MagicMock()

        with (
            patch(
                "custom_components.hsem.device_migration.er.async_get",
                return_value=entity_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.dr.async_get",
                return_value=device_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.er.async_entries_for_config_entry"
            ) as mock_entries,
        ):
            await async_migrate_devices(hass, config_entry)

        mock_entries.assert_not_called()
        entity_reg.async_update_entity.assert_not_called()
        hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_entity_id_rename_uses_registry_primitive(self) -> None:
        """The rename primitive is exercised so statistic-following is guaranteed.

        No production entity needs a rename today (unique_id and entity_id
        getters were kept frozen — see the module docstring), but the
        mechanism is real and tested: renaming via
        ``entity_registry.async_update_entity(..., new_entity_id=...)`` is
        the exact call HA's recorder listens to when following long-term
        statistics to a renamed ``statistic_id``.
        """
        hass = _mock_hass()
        config_entry = _mock_config_entry()

        old_unique_id = f"{DOMAIN}_{_ENTRY_ID}_export_income_sensor"
        old_entity_id = "sensor.hsem_export_income_old"
        new_entity_id = "sensor.hsem_export_income"

        entity_entries = [
            _make_entity_entry(
                old_entity_id, old_unique_id, device_id="device-financial"
            ),
        ]

        entity_reg = MagicMock()
        device_reg = MagicMock()
        device = MagicMock()
        device.id = "device-financial"
        device_reg.async_get_or_create.return_value = device

        with (
            patch(
                "custom_components.hsem.device_migration.er.async_get",
                return_value=entity_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.dr.async_get",
                return_value=device_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.er.async_entries_for_config_entry",
                return_value=entity_entries,
            ),
            patch.dict(_ENTITY_ID_RENAMES, {old_unique_id: new_entity_id}),
        ):
            await async_migrate_devices(hass, config_entry)

        entity_reg.async_update_entity.assert_called_once_with(
            old_entity_id, new_entity_id=new_entity_id
        )

    @pytest.mark.asyncio
    async def test_unique_id_is_never_included_in_migration_updates(self) -> None:
        """Defensive regression guard: the migration must never rewrite unique_id."""
        hass = _mock_hass()
        config_entry = _mock_config_entry()

        entity_entries = [
            _make_entity_entry(
                "sensor.hsem_export_income",
                f"{DOMAIN}_{_ENTRY_ID}_export_income_sensor",
                device_id="old-device",
            ),
        ]

        entity_reg = MagicMock()
        device_reg = MagicMock()
        device = MagicMock()
        device.id = "device-financial"
        device_reg.async_get_or_create.return_value = device

        with (
            patch(
                "custom_components.hsem.device_migration.er.async_get",
                return_value=entity_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.dr.async_get",
                return_value=device_reg,
            ),
            patch(
                "custom_components.hsem.device_migration.er.async_entries_for_config_entry",
                return_value=entity_entries,
            ),
        ):
            await async_migrate_devices(hass, config_entry)

        for call in entity_reg.async_update_entity.call_args_list:
            assert "new_unique_id" not in call.kwargs
            assert "unique_id" not in call.kwargs
