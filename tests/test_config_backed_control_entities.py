"""Tests for the config-entry-backed control entities.

The EV target-SoC and battery-efficiency numbers, the Solcast likelihood
selector, and the time entities all follow the same pattern: seed their value
from the config entry, re-read it when the options flow changes it, and write
user changes back into ``config_entry.options`` so they survive a restart.
"""

from __future__ import annotations

from datetime import time
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.number import NumberEntityDescription
from homeassistant.components.select import SelectEntityDescription
from homeassistant.helpers.entity import Entity

from custom_components.hsem.custom_numbers.battery_efficiency import (
    HSEMBatteryEfficiencyNumber,
)
from custom_components.hsem.custom_numbers.ev_target_soc import HSEMEVTargetSocNumber
from custom_components.hsem.custom_selectors.solcast_likelihood import (
    HSEMSolcastLikelihoodSelector,
)
from custom_components.hsem.custom_times.description import HSEMTimeEntityDescription
from custom_components.hsem.custom_times.time import HSEMTimeEntity
from custom_components.hsem.devices import HSEMDevice
from custom_components.hsem.utils.solcast_likelihood import (
    DEFAULT_SOLCAST_LIKELIHOOD,
    SOLCAST_LIKELIHOOD_OPTIONS,
)

_TARGET_SOC_KEY = "hsem_ev_target_soc"
_EFFICIENCY_KEY = "hsem_batteries_charge_efficiency"
_LIKELIHOOD_KEY = "hsem_solcast_pv_forecast_forecast_likelihood"
_DEADLINE_KEY = "hsem_ev_deadline_time"


def _entry(options: dict[str, Any] | None = None) -> MagicMock:
    """Return a config entry whose options can be replaced by the entity."""
    entry = MagicMock()
    entry.entry_id = "test_entry"
    entry.options = dict(options or {})
    entry.data = {}
    return entry


def _hass() -> MagicMock:
    """Return a Home Assistant mock recording config entry updates."""
    hass = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _written_options(hass: MagicMock) -> dict[str, Any]:
    """Return the options dict the entity persisted to the config entry."""
    return dict(hass.config_entries.async_update_entry.call_args.kwargs["options"])


def _silence_state_writes(entity: Entity) -> None:
    """Replace ``async_write_ha_state`` so no HA runtime is needed."""
    entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign, misc]  # HA marks it final


def _update_listener(entity: Any) -> MagicMock:
    """Return the config entry's ``add_update_listener`` mock."""
    return cast(MagicMock, entity._config_entry.add_update_listener)


def _number(
    sensor_cls: Any,
    config_key: str,
    *,
    options: dict[str, Any] | None = None,
    default: float,
) -> tuple[Any, MagicMock]:
    """Return a number entity plus the ``hass`` mock it writes through."""
    hass = _hass()
    entity = sensor_cls(
        hass,
        _entry(options),
        NumberEntityDescription(key=config_key, name="Test number"),
        config_key,
        default,
    )
    _silence_state_writes(entity)
    return entity, hass


_NUMBER_CASES = [
    pytest.param(
        HSEMEVTargetSocNumber, _TARGET_SOC_KEY, 80.0, 1.0, 100.0, id="ev_target_soc"
    ),
    pytest.param(
        HSEMBatteryEfficiencyNumber,
        _EFFICIENCY_KEY,
        98.0,
        50.0,
        100.0,
        id="battery_efficiency",
    ),
]


class TestNumberEntities:
    """Numbers clamp to their range and persist to the config entry."""

    @pytest.mark.parametrize(
        ("sensor_cls", "config_key", "default", "minimum", "maximum"), _NUMBER_CASES
    )
    def test_stored_value_is_loaded(
        self,
        sensor_cls: Any,
        config_key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> None:
        """A saved option seeds the entity value."""
        entity, _hass_mock = _number(
            sensor_cls, config_key, options={config_key: 66}, default=default
        )

        assert entity.native_value == pytest.approx(66.0)
        assert entity.native_min_value == pytest.approx(minimum)
        assert entity.native_max_value == pytest.approx(maximum)
        assert entity.name == "Test number"

    @pytest.mark.parametrize(
        ("sensor_cls", "config_key", "default", "minimum", "maximum"), _NUMBER_CASES
    )
    def test_unset_option_uses_the_default(
        self,
        sensor_cls: Any,
        config_key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> None:
        """Without a usable stored value the documented default applies."""
        entity, _hass_mock = _number(
            sensor_cls,
            config_key,
            options={config_key: "not a number"},
            default=default,
        )

        assert entity.native_value == pytest.approx(default)

    @pytest.mark.parametrize(
        ("sensor_cls", "config_key", "default", "minimum", "maximum"), _NUMBER_CASES
    )
    @pytest.mark.asyncio
    async def test_setting_a_value_persists_it(
        self,
        sensor_cls: Any,
        config_key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> None:
        """A user change is written back into the config entry options."""
        entity, hass = _number(
            sensor_cls, config_key, options={"other_key": 1}, default=default
        )

        await entity.async_set_native_value(default - 1)

        assert entity.native_value == pytest.approx(default - 1)
        written = _written_options(hass)
        assert written[config_key] == pytest.approx(default - 1)
        # Unrelated options are preserved.
        assert written["other_key"] == 1

    @pytest.mark.parametrize(
        ("sensor_cls", "config_key", "default", "minimum", "maximum"), _NUMBER_CASES
    )
    @pytest.mark.asyncio
    async def test_out_of_range_values_are_clamped(
        self,
        sensor_cls: Any,
        config_key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> None:
        """Values beyond the slider range are clamped before being stored."""
        entity, hass = _number(sensor_cls, config_key, default=default)

        await entity.async_set_native_value(maximum + 50)
        assert entity.native_value == pytest.approx(maximum)

        await entity.async_set_native_value(minimum - 50)
        assert entity.native_value == pytest.approx(minimum)
        assert _written_options(hass)[config_key] == pytest.approx(minimum)

    @pytest.mark.parametrize(
        ("sensor_cls", "config_key", "default", "minimum", "maximum"), _NUMBER_CASES
    )
    @pytest.mark.asyncio
    async def test_options_flow_change_is_picked_up(
        self,
        sensor_cls: Any,
        config_key: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> None:
        """An options-flow save refreshes the entity value."""
        entity, hass = _number(sensor_cls, config_key, default=default)
        entity.async_on_remove = MagicMock()  # type: ignore[method-assign]  # test monkey-patch

        with patch.object(Entity, "async_added_to_hass", AsyncMock()):
            await entity.async_added_to_hass()
        _update_listener(entity).assert_called_once()

        await entity._async_handle_config_update(
            hass, _entry({config_key: minimum + 2})
        )
        assert entity.native_value == pytest.approx(minimum + 2)

        await entity._async_handle_config_update(hass, _entry())
        assert entity.native_value == pytest.approx(default)

    def test_second_ev_target_soc_binds_to_the_secondary_device(self) -> None:
        """The second EV's number attaches to the EV Secondary device."""
        primary = HSEMEVTargetSocNumber(
            _hass(),
            _entry(),
            NumberEntityDescription(key=_TARGET_SOC_KEY),
            _TARGET_SOC_KEY,
        )
        second = HSEMEVTargetSocNumber(
            _hass(),
            _entry(),
            NumberEntityDescription(key="hsem_ev_second_target_soc"),
            "hsem_ev_second_target_soc",
            is_second=True,
        )

        assert primary._hsem_device is HSEMDevice.EV_PRIMARY
        assert second._hsem_device is HSEMDevice.EV_SECONDARY
        assert primary.device_info != second.device_info

    def test_unnamed_description_defers_to_translations(self) -> None:
        """Without an explicit name the translation key resolves it."""
        entity = HSEMEVTargetSocNumber(
            _hass(),
            _entry(),
            NumberEntityDescription(key=_TARGET_SOC_KEY),
            _TARGET_SOC_KEY,
        )

        assert "_attr_name" not in vars(entity)


def _selector(
    options: dict[str, Any] | None = None,
) -> tuple[HSEMSolcastLikelihoodSelector, MagicMock]:
    """Return the Solcast selector plus the ``hass`` mock behind it."""
    hass = _hass()
    entity = HSEMSolcastLikelihoodSelector(
        hass,
        _entry(options),
        SelectEntityDescription(
            key=_LIKELIHOOD_KEY,
            name="Likelihood",
            options=list(SOLCAST_LIKELIHOOD_OPTIONS),
        ),
    )
    _silence_state_writes(entity)
    return entity, hass


class TestSolcastLikelihoodSelector:
    """The selector persists one of the documented likelihood options."""

    def test_stored_option_is_loaded(self) -> None:
        """A saved option seeds the current selection."""
        option = next(
            o for o in SOLCAST_LIKELIHOOD_OPTIONS if o != DEFAULT_SOLCAST_LIKELIHOOD
        )
        entity, _hass_mock = _selector({_LIKELIHOOD_KEY: option})

        assert entity.current_option == option
        assert entity.options == list(SOLCAST_LIKELIHOOD_OPTIONS)

    def test_unknown_stored_option_falls_back_to_the_default(self) -> None:
        """An option no longer offered is replaced by the default."""
        entity, _hass_mock = _selector({_LIKELIHOOD_KEY: "retired_option"})

        assert entity.current_option == DEFAULT_SOLCAST_LIKELIHOOD

    @pytest.mark.asyncio
    async def test_selecting_an_option_persists_it(self) -> None:
        """A user selection is written back into the config entry options."""
        option = next(
            o for o in SOLCAST_LIKELIHOOD_OPTIONS if o != DEFAULT_SOLCAST_LIKELIHOOD
        )
        entity, hass = _selector({"other_key": 1})

        await entity.async_select_option(option)

        assert entity.current_option == option
        written = _written_options(hass)
        assert written[_LIKELIHOOD_KEY] == option
        assert written["other_key"] == 1

    @pytest.mark.asyncio
    async def test_unknown_option_is_rejected(self) -> None:
        """An option outside the offered list is refused, not persisted."""
        entity, hass = _selector()

        with pytest.raises(ValueError, match="Invalid option"):
            await entity.async_select_option("retired_option")

        hass.config_entries.async_update_entry.assert_not_called()

    @pytest.mark.asyncio
    async def test_options_flow_change_is_picked_up(self) -> None:
        """An options-flow save refreshes the current selection."""
        option = next(
            o for o in SOLCAST_LIKELIHOOD_OPTIONS if o != DEFAULT_SOLCAST_LIKELIHOOD
        )
        entity, hass = _selector()
        entity.async_on_remove = MagicMock()  # type: ignore[method-assign]  # test monkey-patch

        with patch.object(Entity, "async_added_to_hass", AsyncMock()):
            await entity.async_added_to_hass()
        _update_listener(entity).assert_called_once()

        await entity._async_handle_config_update(
            hass, _entry({_LIKELIHOOD_KEY: option})
        )
        assert entity.current_option == option

        await entity._async_handle_config_update(
            hass, _entry({_LIKELIHOOD_KEY: "retired_option"})
        )
        assert entity.current_option == DEFAULT_SOLCAST_LIKELIHOOD


class TestTimeEntity:
    """The time entities keep their value in sync with the config entry."""

    @staticmethod
    def _time_entity(options: dict[str, Any] | None = None) -> HSEMTimeEntity:
        """Return an EV deadline time entity."""
        entity = HSEMTimeEntity(
            _hass(),
            _entry(options),
            HSEMTimeEntityDescription(key=_DEADLINE_KEY, name="Deadline"),
        )
        _silence_state_writes(entity)
        return entity

    @pytest.mark.asyncio
    async def test_options_flow_change_is_picked_up(self) -> None:
        """An options-flow save refreshes the published time."""
        entity = self._time_entity({_DEADLINE_KEY: "07:00:00"})
        entity.async_on_remove = MagicMock()  # type: ignore[method-assign]  # test monkey-patch

        with patch.object(Entity, "async_added_to_hass", AsyncMock()):
            await entity.async_added_to_hass()
        _update_listener(entity).assert_called_once()

        await entity._async_handle_config_update(
            _hass(), _entry({_DEADLINE_KEY: "06:30:00"})
        )

        assert entity.native_value == time(6, 30)
