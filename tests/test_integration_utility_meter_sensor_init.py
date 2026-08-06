"""Regression tests for HSEMIntegrationSensor / HSEMUtilityMeterSensor construction
against both the pre-2026.8 and 2026.8+ Home Assistant core signatures.

home-assistant/core PR #177596 ("Do not set a device on YAML integration
entities", merged 2026-07-30) removed the `hass` parameter from
``IntegrationSensor.__init__``, and PR #177603 ("Do not set a device on YAML
utility_meter entities", merged the same day) made the identical change to
``UtilityMeterSensor.__init__``. Neither change was listed in HA's official
breaking-changes documentation, because both classes are considered internal
API by core maintainers. HSEM instantiates both classes via keyword-only
``hass=self.hass`` in
``custom_components/hsem/custom_sensors/house_consumption_power_sensor.py``,
so on HA 2026.8+ this raised ``TypeError: __init__() got an unexpected
keyword argument 'hass'`` at entity-creation time.

The fix (in ``integration_sensor.py`` and ``utility_meter_sensor.py``)
inspects the installed parent class's ``__init__`` signature at runtime and
only forwards ``hass`` when the signature still accepts it.

IMPORTANT: unlike ``tests/test_house_consumption_sensor_lifecycle.py``, the
tests below do NOT monkeypatch ``HSEMIntegrationSensor.__init__`` or
``HSEMUtilityMeterSensor.__init__`` themselves — that would bypass the real
constructor logic entirely and could never catch this class of bug. Instead:

* ``TestRealConstructor`` calls the real wrapper constructors against
  whatever ``homeassistant`` version is actually installed in this
  environment (unpatched parent classes).
* ``TestSimulatedHa2026_8Signature`` patches only the *parent* HA class's
  ``__init__`` (``IntegrationSensor.__init__`` / ``UtilityMeterSensor.__init__``)
  with a stand-in that mirrors the real 2026.8+ signature (no ``hass``
  parameter). This exercises the real HSEM wrapper code deterministically,
  regardless of which ``homeassistant`` version happens to be pinned in
  ``requirements.txt`` at test time.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

from homeassistant.components.integration.sensor import IntegrationSensor
from homeassistant.components.utility_meter.sensor import UtilityMeterSensor
from homeassistant.const import UnitOfTime

from custom_components.hsem.custom_sensors.integration_sensor import (
    HSEMIntegrationSensor,
)
from custom_components.hsem.custom_sensors.utility_meter_sensor import (
    HSEMUtilityMeterSensor,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry() -> MagicMock:
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    return entry


def _integration_sensor_kwargs(hass: Any, config_entry: Any) -> dict[str, Any]:
    """Mirror the real call site in house_consumption_power_sensor.py."""
    return {
        "integration_method": "left",
        "name": "integral test sensor",
        "round_digits": 2,
        "source_entity": "sensor.house_power",
        "unique_id": "integral_unique_id",
        "unit_prefix": "k",
        "unit_time": UnitOfTime.HOURS,
        "max_sub_interval": timedelta(minutes=0),
        "hass": hass,
        "e_id": "sensor.integral_test",
        "id": "integral_unique_id",
        "config_entry": config_entry,
    }


def _utility_meter_sensor_kwargs(hass: Any, config_entry: Any) -> dict[str, Any]:
    """Mirror the real call site in house_consumption_power_sensor.py."""
    return {
        "cron_pattern": None,
        "delta_values": False,
        "meter_offset": timedelta(hours=14),
        "meter_type": "daily",
        "name": "utility meter test sensor",
        "net_consumption": True,
        "parent_meter": "sensor.house_power",
        "periodically_resetting": True,
        "source_entity": "sensor.integral_test",
        "tariff_entity": None,
        "tariff": None,
        "unique_id": "utility_unique_id",
        "hass": hass,
        "sensor_always_available": True,
        "id": "utility_unique_id",
        "e_id": "sensor.utility_test",
        "config_entry": config_entry,
    }


def _fake_2026_8_integration_init(
    self: Any,
    *,
    integration_method: str,
    name: str | None,
    round_digits: int | None,
    source_entity: str,
    unique_id: str | None,
    unit_prefix: str | None,
    unit_time: Any,
    max_sub_interval: timedelta | None,
    device: Any = None,
) -> None:
    """Stand-in for HA 2026.8+ ``IntegrationSensor.__init__`` (no ``hass``).

    Records the kwargs it actually received so the test can assert that
    ``hass`` was stripped before reaching this point, without needing HA
    2026.8 installed.
    """
    self._attr_unique_id = unique_id
    self._received_kwargs = {
        "integration_method": integration_method,
        "name": name,
        "round_digits": round_digits,
        "source_entity": source_entity,
        "unique_id": unique_id,
        "unit_prefix": unit_prefix,
        "unit_time": unit_time,
        "max_sub_interval": max_sub_interval,
        "device": device,
    }


def _fake_2026_8_utility_init(
    self: Any,
    *,
    cron_pattern: str | None,
    delta_values: bool,
    meter_offset: timedelta,
    meter_type: str | None,
    name: str | None,
    net_consumption: bool,
    parent_meter: str,
    periodically_resetting: bool,
    source_entity: str,
    tariff_entity: str | None,
    tariff: str | None,
    unique_id: str | None,
    sensor_always_available: bool = False,
    suggested_entity_id: str | None = None,
    device: Any = None,
) -> None:
    """Stand-in for HA 2026.8+ ``UtilityMeterSensor.__init__`` (no ``hass``)."""
    self._attr_unique_id = unique_id
    self._received_kwargs = {
        "cron_pattern": cron_pattern,
        "delta_values": delta_values,
        "meter_offset": meter_offset,
        "meter_type": meter_type,
        "name": name,
        "net_consumption": net_consumption,
        "parent_meter": parent_meter,
        "periodically_resetting": periodically_resetting,
        "source_entity": source_entity,
        "tariff_entity": tariff_entity,
        "tariff": tariff,
        "unique_id": unique_id,
        "sensor_always_available": sensor_always_available,
        "suggested_entity_id": suggested_entity_id,
        "device": device,
    }


# ---------------------------------------------------------------------------
# Real constructor, unpatched — proves no regression against whichever
# homeassistant version is actually installed (pinned in requirements.txt).
# ---------------------------------------------------------------------------


class TestRealConstructor:
    """Construct the real HSEM wrapper classes with no __init__ patching."""

    def test_integration_sensor_constructs_without_typeerror(self) -> None:
        hass = MagicMock()
        config_entry = _mock_config_entry()

        sensor = HSEMIntegrationSensor(**_integration_sensor_kwargs(hass, config_entry))

        assert sensor.unique_id == "integral_unique_id"
        assert sensor.entity_id == "sensor.integral_test"

    def test_utility_meter_sensor_constructs_without_typeerror(self) -> None:
        hass = MagicMock()
        config_entry = _mock_config_entry()

        sensor = HSEMUtilityMeterSensor(
            **_utility_meter_sensor_kwargs(hass, config_entry)
        )

        assert sensor.unique_id == "utility_unique_id"
        assert sensor.entity_id == "sensor.utility_test"


# ---------------------------------------------------------------------------
# Simulated HA 2026.8+ signature — deterministic, independent of the
# installed homeassistant version. Patches only the parent HA class's
# __init__, so the real HSEM wrapper __init__ (including the fix) still runs.
# ---------------------------------------------------------------------------


class TestSimulatedHa2026_8Signature:
    """HSEM wrapper must not forward `hass` when the parent HA class rejects it."""

    def test_integration_sensor_drops_hass_when_parent_rejects_it(self) -> None:
        hass = MagicMock()
        config_entry = _mock_config_entry()

        with patch.object(IntegrationSensor, "__init__", _fake_2026_8_integration_init):
            sensor = HSEMIntegrationSensor(
                **_integration_sensor_kwargs(hass, config_entry)
            )

        assert sensor.unique_id == "integral_unique_id"
        assert sensor.entity_id == "sensor.integral_test"
        assert "hass" not in sensor._received_kwargs  # type: ignore[attr-defined]  # set by fake init in test

    def test_utility_meter_sensor_drops_hass_when_parent_rejects_it(self) -> None:
        hass = MagicMock()
        config_entry = _mock_config_entry()

        with patch.object(UtilityMeterSensor, "__init__", _fake_2026_8_utility_init):
            sensor = HSEMUtilityMeterSensor(
                **_utility_meter_sensor_kwargs(hass, config_entry)
            )

        assert sensor.unique_id == "utility_unique_id"
        assert sensor.entity_id == "sensor.utility_test"
        assert "hass" not in sensor._received_kwargs  # type: ignore[attr-defined]  # set by fake init in test

    def test_integration_sensor_still_passes_hass_when_parent_accepts_it(
        self,
    ) -> None:
        """Backward-compat guard: pre-2026.8 HA still requires `hass`.

        The fake's parameter must literally be named ``hass`` — that name is
        exactly what ``inspect.signature(...).parameters`` is checked against
        in the fix, so this pins down that detail too.
        """
        hass_mock = MagicMock()
        config_entry = _mock_config_entry()
        received: dict[str, Any] = {}

        def fake_pre_2026_8_init(self: Any, hass: Any, **kwargs: Any) -> None:  # noqa: ANN001 - test stand-in
            received["hass"] = hass
            self._attr_unique_id = kwargs["unique_id"]

        with patch.object(IntegrationSensor, "__init__", fake_pre_2026_8_init):
            HSEMIntegrationSensor(**_integration_sensor_kwargs(hass_mock, config_entry))

        assert received["hass"] is hass_mock

    def test_utility_meter_sensor_still_passes_hass_when_parent_accepts_it(
        self,
    ) -> None:
        """Backward-compat guard: pre-2026.8 HA still requires `hass`.

        The fake's parameter must literally be named ``hass`` — that name is
        exactly what ``inspect.signature(...).parameters`` is checked against
        in the fix, so this pins down that detail too.
        """
        hass_mock = MagicMock()
        config_entry = _mock_config_entry()
        received: dict[str, Any] = {}

        def fake_pre_2026_8_init(self: Any, hass: Any, **kwargs: Any) -> None:  # noqa: ANN001 - test stand-in
            received["hass"] = hass
            self._attr_unique_id = kwargs["unique_id"]

        with patch.object(UtilityMeterSensor, "__init__", fake_pre_2026_8_init):
            HSEMUtilityMeterSensor(
                **_utility_meter_sensor_kwargs(hass_mock, config_entry)
            )

        assert received["hass"] is hass_mock
