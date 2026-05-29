"""Tests for HSEM entity platform class correctness (issue #285).

Verifies that each entity class inherits from the correct Home Assistant base
class so that HA's platform dispatching works as intended:

- ``HSEMTimeEntity`` → :class:`homeassistant.components.time.TimeEntity`
- ``HSEMSwitch``     → :class:`homeassistant.components.switch.SwitchEntity`
- ``HSEMWorkingModeSelector`` → :class:`homeassistant.components.select.SelectEntity`

Also exercises entity construction and the ``async_set_value`` / state round-trip
for time entities without requiring a live Home Assistant instance.
"""

from __future__ import annotations

from datetime import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity
from homeassistant.helpers.entity import ToggleEntity

from custom_components.hsem.custom_selectors.working_mode import HSEMWorkingModeSelector
from custom_components.hsem.custom_switches.switch import HSEMSwitch
from custom_components.hsem.custom_times.description import HSEMTimeEntityDescription
from custom_components.hsem.custom_times.time import HSEMTimeEntity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry(**option_overrides: object) -> MagicMock:
    """Return a minimal mock ConfigEntry.

    Parameters
    ----------
    option_overrides:
        Extra option key/value pairs to include in ``options``.
    """
    entry = MagicMock()
    entry.entry_id = "test_entry_id"
    entry.options = {
        "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
        **option_overrides,
    }
    return entry


def _mock_hass() -> MagicMock:
    """Return a minimal Home Assistant mock."""
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


# ---------------------------------------------------------------------------
# Inheritance audit
# ---------------------------------------------------------------------------


class TestEntityBaseClasses:
    """Verify each entity class inherits from the correct HA base class."""

    def test_time_entity_inherits_from_time_entity(self) -> None:
        """HSEMTimeEntity must extend TimeEntity, not ToggleEntity."""
        assert issubclass(HSEMTimeEntity, TimeEntity), (
            "HSEMTimeEntity should inherit from homeassistant.components.time.TimeEntity"
        )

    def test_time_entity_does_not_inherit_from_toggle_entity(self) -> None:
        """HSEMTimeEntity must NOT extend ToggleEntity (the old incorrect base)."""
        assert not issubclass(HSEMTimeEntity, ToggleEntity), (
            "HSEMTimeEntity must not inherit from ToggleEntity"
        )

    def test_switch_entity_inherits_from_switch_entity(self) -> None:
        """HSEMSwitch must extend SwitchEntity."""
        assert issubclass(HSEMSwitch, SwitchEntity)

    def test_selector_inherits_from_select_entity(self) -> None:
        """HSEMWorkingModeSelector must extend SelectEntity."""
        assert issubclass(HSEMWorkingModeSelector, SelectEntity)


# ---------------------------------------------------------------------------
# HSEMTimeEntity — construction
# ---------------------------------------------------------------------------


class TestHSEMTimeEntityConstruction:
    """Test that HSEMTimeEntity initialises correctly from a string default."""

    def _make_entity(
        self,
        default: str = "07:00:00",
        key: str = "hsem_batteries_enable_batteries_schedule_1_start",
    ) -> HSEMTimeEntity:
        hass = _mock_hass()
        config_entry = _mock_config_entry()
        description = HSEMTimeEntityDescription(
            key=key,
            name="Batteries Discharge Schedule 1 Start",
            description="Start time for schedule 1.",
            default_value=default,
        )
        return HSEMTimeEntity(hass, config_entry, description)

    def test_native_value_is_time_object(self) -> None:
        """native_value must be a datetime.time after construction."""
        entity = self._make_entity("07:00:00")
        assert isinstance(entity.native_value, time)

    def test_native_value_hh_mm_ss_parsed_correctly(self) -> None:
        """'HH:MM:SS' strings are parsed to the correct time."""
        entity = self._make_entity("14:30:00")
        assert entity.native_value == time(14, 30, 0)

    def test_native_value_hh_mm_parsed_correctly(self) -> None:
        """'HH:MM' strings (without seconds) are also accepted."""
        entity = self._make_entity("09:00")
        assert entity.native_value == time(9, 0, 0)

    def test_native_value_midnight(self) -> None:
        """Midnight '00:00:00' parses to time(0, 0, 0)."""
        entity = self._make_entity("00:00:00")
        assert entity.native_value == time(0, 0, 0)

    def test_native_value_end_of_day(self) -> None:
        """'23:59:59' is a valid time boundary."""
        entity = self._make_entity("23:59:59")
        assert entity.native_value == time(23, 59, 59)

    def test_native_value_empty_string_gives_none(self) -> None:
        """An empty default string results in native_value=None."""
        entity = self._make_entity("")
        assert entity.native_value is None

    def test_native_value_invalid_string_gives_none(self) -> None:
        """A non-parseable string results in native_value=None (safe fallback)."""
        entity = self._make_entity("not-a-time")
        assert entity.native_value is None

    def test_state_property_returns_iso_string(self) -> None:
        """state must return an ISO-format string as required by TimeEntity."""
        entity = self._make_entity("07:30:00")
        assert entity.state == "07:30:00"

    def test_unique_id_contains_key(self) -> None:
        """unique_id is derived from the config-entry key."""
        key = "hsem_batteries_enable_batteries_schedule_1_start"
        entity = self._make_entity(key=key)
        assert entity.unique_id is not None
        assert key in entity.unique_id

    def test_unique_id_ends_with_time_suffix(self) -> None:
        """unique_id uses the '_time' suffix to avoid collisions with other platforms."""
        entity = self._make_entity()
        assert entity.unique_id is not None
        assert entity.unique_id.endswith("_time")

    def test_extra_state_attributes_contains_description(self) -> None:
        """extra_state_attributes must include a 'description' key."""
        entity = self._make_entity()
        attrs = entity.extra_state_attributes
        assert "description" in attrs
        assert isinstance(attrs["description"], str)

    def test_icon(self) -> None:
        """The clock icon is set."""
        entity = self._make_entity()
        assert entity.icon == "mdi:clock"


# ---------------------------------------------------------------------------
# HSEMTimeEntity — async_set_value round-trip
# ---------------------------------------------------------------------------


class TestHSEMTimeEntitySetValue:
    """Test async_set_value persists the new time and updates the config entry."""

    def _make_entity(self) -> HSEMTimeEntity:
        hass = _mock_hass()
        config_entry = _mock_config_entry()
        description = HSEMTimeEntityDescription(
            key="hsem_batteries_enable_batteries_schedule_1_start",
            name="Batteries Discharge Schedule 1 Start",
            description="Start time for schedule 1.",
            default_value="07:00:00",
        )
        return HSEMTimeEntity(hass, config_entry, description)

    @pytest.mark.asyncio
    async def test_set_value_updates_native_value(self) -> None:
        """native_value reflects the time passed to async_set_value."""
        entity = self._make_entity()
        entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        new_time = time(17, 0, 0)
        await entity.async_set_value(new_time)
        assert entity.native_value == new_time

    @pytest.mark.asyncio
    async def test_set_value_persists_to_config_entry(self) -> None:
        """Config entry is updated with the ISO string of the new time."""
        entity = self._make_entity()
        entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        new_time = time(21, 30, 0)
        await entity.async_set_value(new_time)
        entity.hass.config_entries.async_update_entry.assert_called_once()
        call_kwargs = entity.hass.config_entries.async_update_entry.call_args
        updated_options = call_kwargs[1]["options"]
        assert (
            updated_options["hsem_batteries_enable_batteries_schedule_1_start"]
            == "21:30:00"
        )

    @pytest.mark.asyncio
    async def test_set_value_calls_async_write_ha_state(self) -> None:
        """async_write_ha_state is called after persisting to push the update to HA."""
        entity = self._make_entity()
        entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
        await entity.async_set_value(time(8, 0, 0))
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_value_multiple_times(self) -> None:
        """Multiple sequential calls each update native_value correctly."""
        entity = self._make_entity()
        entity.async_write_ha_state = MagicMock()  # type: ignore[method-assign]

        for hour in (9, 12, 23):
            new_time = time(hour, 0, 0)
            await entity.async_set_value(new_time)
            assert entity.native_value == new_time


# ---------------------------------------------------------------------------
# HSEMTimeEntity._parse_time — static helper
# ---------------------------------------------------------------------------


class TestParseTime:
    """Unit tests for the :meth:`HSEMTimeEntity._parse_time` static helper."""

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("00:00:00", time(0, 0, 0)),
            ("07:00:00", time(7, 0, 0)),
            ("09:00", time(9, 0, 0)),
            ("23:59:59", time(23, 59, 59)),
            ("12:30:45", time(12, 30, 45)),
            ("", None),
            ("not-a-time", None),
            ("99:99:99", None),
        ],
    )
    def test_parse_time_parametrized(self, value: str, expected: time | None) -> None:
        """_parse_time returns the expected datetime.time or None."""
        assert HSEMTimeEntity._parse_time(value) == expected


# ---------------------------------------------------------------------------
# time.py platform setup — entity count and types
# ---------------------------------------------------------------------------


class TestTimePlatformSetup:
    """Verify that async_setup_entry registers the expected time entities."""

    @pytest.mark.asyncio
    async def test_setup_entry_creates_eight_time_entities(self) -> None:
        """Eight time entities (3 schedules + 2 EV deadlines) should be created."""
        from custom_components.hsem.time import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry(
            **{
                "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
                "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
                "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
                "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
                "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
                "hsem_ev_deadline_time": "07:00:00",
                "hsem_ev_second_deadline_time": "07:00:00",
            }
        )

        added: list[HSEMTimeEntity] = []

        def add_entities(entities: Any, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, "00:00:00"),
        ):
            await async_setup_entry(hass, config_entry, add_entities)  # type: ignore[arg-type]  # HA AddEntitiesCallback stub too strict for test callback

        assert len(added) == 8

    @pytest.mark.asyncio
    async def test_setup_entry_all_entities_are_time_entities(self) -> None:
        """Every entity registered by async_setup_entry is an HSEMTimeEntity instance."""
        from custom_components.hsem.time import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry(
            **{
                "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
                "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
                "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
                "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
                "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
            }
        )
        added: list = []

        def add_entities(entities: Any, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, "00:00:00"),
        ):
            await async_setup_entry(hass, config_entry, add_entities)  # type: ignore[arg-type]  # HA AddEntitiesCallback stub too strict for test callback

        for entity in added:
            assert isinstance(entity, HSEMTimeEntity)
            assert isinstance(entity, TimeEntity)

    @pytest.mark.asyncio
    async def test_setup_entry_entities_have_valid_native_values(self) -> None:
        """Every time entity has a non-None native_value after setup."""
        from custom_components.hsem.time import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry(
            **{
                "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
                "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
                "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
                "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
                "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
                "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
                "hsem_ev_deadline_time": "07:00:00",
                "hsem_ev_second_deadline_time": "07:00:00",
            }
        )
        added: list = []

        def add_entities(entities: Any, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, "00:00:00"),
        ):
            await async_setup_entry(hass, config_entry, add_entities)  # type: ignore[arg-type]  # HA AddEntitiesCallback stub too strict for test callback

        for entity in added:
            assert entity.native_value is not None
            assert isinstance(entity.native_value, time)


# ---------------------------------------------------------------------------
# HSEMTimeEntity — async_set_value round-trip
