"""Tests for the entity-float normalization helpers in ``utils/ha_helpers.py``.

Verifies :func:`normalize_entity_float` (a thin wrapper around
:func:`custom_components.hsem.utils.unit_normalize.normalize_to_unit` that
reads an entity's declared ``unit_of_measurement`` and normalizes an
already-converted float reading) and :func:`read_normalized_float` (which
additionally invokes a caller-supplied read closure) — used by
``state_collector.py`` and ``coordinator_live_power.py`` for grid/PV energy
and house/solar/phase power meters (issue #946).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from homeassistant.const import UnitOfEnergy, UnitOfPower

from custom_components.hsem.utils.ha_helpers import (
    normalize_entity_float,
    read_normalized_float,
)


def _hass(unit: str | None) -> MagicMock:
    hass = MagicMock()
    state = MagicMock()
    state.attributes = {"unit_of_measurement": unit} if unit is not None else {}
    hass.states.get.return_value = state
    return hass


class TestNormalizeEntityFloat:
    """Unit tests for the generic entity-float normalizer."""

    def test_none_value_returns_none(self) -> None:
        result = normalize_entity_float(
            MagicMock(hass=_hass("kW")),
            "sensor.house",
            None,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result is None

    def test_none_entity_id_returns_value_unchanged(self) -> None:
        result = normalize_entity_float(
            MagicMock(hass=_hass("kW")),
            None,
            123.0,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(123.0)

    def test_kw_power_normalized_to_watts(self) -> None:
        """A house-power template sensor reporting kW must scale to Watts."""
        result = normalize_entity_float(
            MagicMock(hass=_hass("kW")),
            "sensor.house",
            3.6,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(3600.0)

    def test_watts_passthrough(self) -> None:
        result = normalize_entity_float(
            MagicMock(hass=_hass("W")),
            "sensor.house",
            500.0,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(500.0)

    def test_wh_energy_normalized_to_kwh(self) -> None:
        """A grid-import meter reporting Wh must scale down to kWh."""
        result = normalize_entity_float(
            MagicMock(hass=_hass("Wh")),
            "sensor.grid_import",
            15000.0,
            UnitOfEnergy.KILO_WATT_HOUR,
            label="grid_import_energy",
        )
        assert result == pytest.approx(15.0)

    def test_kwh_passthrough(self) -> None:
        result = normalize_entity_float(
            MagicMock(hass=_hass("kWh")),
            "sensor.grid_import",
            42.5,
            UnitOfEnergy.KILO_WATT_HOUR,
            label="grid_import_energy",
        )
        assert result == pytest.approx(42.5)

    def test_missing_unit_assumes_canonical(self) -> None:
        result = normalize_entity_float(
            MagicMock(hass=_hass(None)),
            "sensor.house",
            750.0,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(750.0)

    def test_missing_hass_attribute_returns_value_unchanged(self) -> None:
        """A caller whose ``self`` has no usable ``.hass`` must not raise."""
        caller = MagicMock()
        caller.hass = None
        result = normalize_entity_float(
            caller,
            "sensor.house",
            750.0,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(750.0)


class TestReadNormalizedFloat:
    """Unit tests for the read-closure + normalize convenience wrapper.

    Grid/PV energy and house/solar/phase power readings must be normalised
    to HSEM's canonical unit (kWh / W), the same class of unit-mismatch
    risk EV charger power already guards against (issue #592).
    """

    def test_kw_power_normalized_to_watts(self) -> None:
        result = read_normalized_float(
            MagicMock(hass=_hass("kW")),
            "sensor.house",
            lambda *a, **k: 3.6,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(3600.0)

    def test_watts_passthrough(self) -> None:
        result = read_normalized_float(
            MagicMock(hass=_hass("W")),
            "sensor.house",
            lambda *a, **k: 500.0,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result == pytest.approx(500.0)

    def test_wh_energy_normalized_to_kwh(self) -> None:
        result = read_normalized_float(
            MagicMock(hass=_hass("Wh")),
            "sensor.grid_import",
            lambda *a, **k: 15000.0,
            UnitOfEnergy.KILO_WATT_HOUR,
            label="grid_import_energy",
        )
        assert result == pytest.approx(15.0)

    def test_missing_unit_assumes_canonical(self) -> None:
        result = read_normalized_float(
            MagicMock(hass=_hass(None)),
            "sensor.pv",
            lambda *a, **k: 12.5,
            UnitOfEnergy.KILO_WATT_HOUR,
            label="pv_energy",
        )
        assert result == pytest.approx(12.5)

    def test_none_reading_propagates(self) -> None:
        result = read_normalized_float(
            MagicMock(hass=_hass("W")),
            "sensor.house",
            lambda *a, **k: None,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )
        assert result is None

    def test_reader_receives_entity_id_and_label(self) -> None:
        """The reader closure must be invoked with (entity_id, "float", label=...)."""
        calls: list[tuple[tuple, dict]] = []

        def _reader(*args: object, **kwargs: object) -> float:
            calls.append((args, kwargs))
            return 42.0

        result = read_normalized_float(
            MagicMock(hass=_hass("W")),
            "sensor.house",
            _reader,
            UnitOfPower.WATT,
            label="house_consumption_power",
        )

        assert result == pytest.approx(42.0)
        assert calls == [
            (("sensor.house", "float"), {"label": "house_consumption_power"})
        ]
