"""Tests for HSEM entity platform refactor (issue #385).

Verifies that the select, switch, and time platform entities:

- inherit from the correct Home Assistant base class
- use ``_attr_*`` attributes instead of ``@property`` overrides
- use ``EntityDescription`` subclasses for declarative entity definitions
- produce stable ``unique_id`` values that match the pre-refactor format
- correctly map config/options values to entity state
- handle platform setup (``async_setup_entry``) correctly
- preserve user-facing entity IDs

Unique-ID format contract (must never change):
  - Switch:  ``"hsem_<key>_switch"``   e.g. ``"hsem_hsem_read_only_switch"``
  - Time:    ``"hsem_<key>_time"``     e.g. ``"hsem_hsem_batteries_enable_batteries_schedule_1_start_time"``
  - Select:  ``"<key>"``               e.g. ``"hsem_force_working_mode"``
    (The key already carries the ``hsem_`` prefix, keeping it consistent with
    the switch/time convention and compatible with state-collector lookups.)
"""

from __future__ import annotations

from datetime import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.time import TimeEntity

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.custom_selectors.working_mode import HSEMWorkingModeSelector
from custom_components.hsem.custom_switches.description import (
    HSEMSwitchEntityDescription,
)
from custom_components.hsem.custom_switches.switch import HSEMSwitch
from custom_components.hsem.custom_times.description import HSEMTimeEntityDescription
from custom_components.hsem.custom_times.time import HSEMTimeEntity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_config_entry(entry_id: str = "test_entry_id", **opts: Any) -> MagicMock:
    """Return a minimal ConfigEntry mock."""
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = {
        "hsem_read_only": False,
        "hsem_extended_attributes": False,
        "hsem_verbose_logging": False,
        "hsem_batteries_enable_batteries_schedule_1": True,
        "hsem_batteries_enable_batteries_schedule_2": False,
        "hsem_batteries_enable_batteries_schedule_3": False,
        "hsem_ev_charger_force_max_discharge_power": False,
        "hsem_batteries_enable_batteries_schedule_1_start": "07:00:00",
        "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
        "hsem_batteries_enable_batteries_schedule_2_start": "17:00:00",
        "hsem_batteries_enable_batteries_schedule_2_end": "21:00:00",
        "hsem_batteries_enable_batteries_schedule_3_start": "23:00:00",
        "hsem_batteries_enable_batteries_schedule_3_end": "02:00:00",
        **opts,
    }
    return entry


def _mock_hass() -> MagicMock:
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _make_switch(
    key: str = "hsem_read_only",
    name: str = "Read Only",
    description: str = "Toggle read-only mode.",
    is_on: bool = False,
) -> HSEMSwitch:
    hass = _mock_hass()
    config_entry = _mock_config_entry(**{key: is_on})
    desc = HSEMSwitchEntityDescription(
        key=key,
        name=name,
        description=description,
    )
    return HSEMSwitch(hass, config_entry, desc)


def _make_time(
    key: str = "hsem_batteries_enable_batteries_schedule_1_start",
    name: str = "Batteries Discharge Schedule 1 Start",
    description: str = "Start time for schedule 1.",
    default: str = "07:00:00",
) -> HSEMTimeEntity:
    hass = _mock_hass()
    config_entry = _mock_config_entry(**{key: default})
    desc = HSEMTimeEntityDescription(
        key=key,
        name=name,
        description=description,
        default_value=default,
    )
    return HSEMTimeEntity(hass, config_entry, desc)


def _make_selector(
    key: str = "hsem_force_working_mode",
    name: str = "Force Working Mode",
    options: list[str] | None = None,
    default: str = "auto",
    entry_id: str = "test_entry_id",
) -> HSEMWorkingModeSelector:
    hass = _mock_hass()
    config_entry = _mock_config_entry(entry_id=entry_id)
    if options is None:
        options = ["auto", "BatteriesChargeGrid", "BatteriesChargeSolar"]
    desc = SelectEntityDescription(
        key=key,
        name=name,
        options=options,
    )
    return HSEMWorkingModeSelector(hass, config_entry, desc, default)


# ===========================================================================
# Base-class correctness
# ===========================================================================


class TestBaseClasses:
    """All three entity types must inherit the correct HA base class."""

    def test_switch_inherits_switch_entity(self) -> None:
        assert issubclass(HSEMSwitch, SwitchEntity)

    def test_time_inherits_time_entity(self) -> None:
        assert issubclass(HSEMTimeEntity, TimeEntity)

    def test_selector_inherits_select_entity(self) -> None:
        assert issubclass(HSEMWorkingModeSelector, SelectEntity)


# ===========================================================================
# Entity description classes
# ===========================================================================


class TestEntityDescriptions:
    """Verify the custom description dataclasses exist and carry extra fields."""

    def test_switch_description_has_description_field(self) -> None:
        desc = HSEMSwitchEntityDescription(
            key="hsem_read_only",
            name="Read Only",
            description="Toggle read-only mode.",
        )
        assert desc.description == "Toggle read-only mode."

    def test_switch_description_is_frozen(self) -> None:
        """Description must be immutable (frozen=True)."""
        desc = HSEMSwitchEntityDescription(key="k", name="N")
        with pytest.raises((AttributeError, TypeError)):
            desc.description = "new"  # type: ignore[misc]

    def test_time_description_has_description_field(self) -> None:
        desc = HSEMTimeEntityDescription(
            key="hsem_batteries_enable_batteries_schedule_1_start",
            name="S1 Start",
            description="Start time for schedule 1.",
            default_value="07:00:00",
        )
        assert desc.description == "Start time for schedule 1."
        assert desc.default_value == "07:00:00"

    def test_time_description_default_value_defaults_to_zero(self) -> None:
        desc = HSEMTimeEntityDescription(key="k", name="N")
        assert desc.default_value == "00:00:00"

    def test_time_description_is_frozen(self) -> None:
        desc = HSEMTimeEntityDescription(key="k", name="N")
        with pytest.raises((AttributeError, TypeError)):
            desc.default_value = "01:00:00"  # type: ignore[misc]


# ===========================================================================
# Switch unique_id stability
# ===========================================================================


class TestSwitchUniqueId:
    """Switch unique_id must be sourced from sensornames getters."""

    def test_unique_id_format(self) -> None:
        """unique_id must equal the canonical value from sensornames."""
        from custom_components.hsem.utils.sensornames import (
            get_read_only_switch_key,
            get_read_only_switch_unique_id,
        )

        entity = _make_switch(key=get_read_only_switch_key())
        assert entity.unique_id == get_read_only_switch_unique_id()

    def test_unique_id_all_switches(self) -> None:
        """Every switch key produces the unique_id from the sensornames getter."""
        from custom_components.hsem.custom_switches.description import _SWITCH_ID_MAP
        from custom_components.hsem.switch import SWITCH_DESCRIPTIONS

        for desc in SWITCH_DESCRIPTIONS:
            entity = _make_switch(key=desc.key, name=str(desc.name))
            expected_uid, _ = _SWITCH_ID_MAP[desc.key]
            assert entity.unique_id == expected_uid

    def test_unique_id_is_attr_not_property(self) -> None:
        """unique_id must be set via _attr_unique_id, not a @property override."""
        from custom_components.hsem.utils.sensornames import get_read_only_switch_key

        entity = _make_switch(key=get_read_only_switch_key())
        assert hasattr(entity, "_attr_unique_id")
        assert entity._attr_unique_id == entity.unique_id

    def test_different_keys_produce_different_unique_ids(self) -> None:
        from custom_components.hsem.utils.sensornames import (
            get_read_only_switch_key,
            get_verbose_logging_switch_key,
        )

        e1 = _make_switch(key=get_read_only_switch_key())
        e2 = _make_switch(key=get_verbose_logging_switch_key())
        assert e1.unique_id != e2.unique_id


# ===========================================================================
# Switch state and config/options mapping
# ===========================================================================


class TestSwitchState:
    """HSEMSwitch reads initial state from config entry and persists changes."""

    def test_initial_state_false(self) -> None:
        entity = _make_switch(key="hsem_read_only", is_on=False)
        assert entity.is_on is False

    def test_initial_state_true(self) -> None:
        entity = _make_switch(key="hsem_read_only", is_on=True)
        assert entity.is_on is True

    @pytest.mark.asyncio
    async def test_turn_on_updates_attr(self) -> None:
        entity = _make_switch(is_on=False)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        assert entity.is_on is True

    @pytest.mark.asyncio
    async def test_turn_off_updates_attr(self) -> None:
        entity = _make_switch(is_on=True)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        assert entity.is_on is False

    @pytest.mark.asyncio
    async def test_turn_on_persists_to_config_entry(self) -> None:
        entity = _make_switch(key="hsem_read_only", is_on=False)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        entity.hass.config_entries.async_update_entry.assert_called_once()
        opts = entity.hass.config_entries.async_update_entry.call_args[1]["options"]
        assert opts["hsem_read_only"] is True

    @pytest.mark.asyncio
    async def test_turn_off_persists_to_config_entry(self) -> None:
        entity = _make_switch(key="hsem_read_only", is_on=True)
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        opts = entity.hass.config_entries.async_update_entry.call_args[1]["options"]
        assert opts["hsem_read_only"] is False

    @pytest.mark.asyncio
    async def test_turn_on_calls_write_ha_state(self) -> None:
        entity = _make_switch()
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_on()
        entity.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_turn_off_calls_write_ha_state(self) -> None:
        entity = _make_switch()
        entity.async_write_ha_state = MagicMock()
        await entity.async_turn_off()
        entity.async_write_ha_state.assert_called_once()

    def test_extra_state_attributes_has_description(self) -> None:
        entity = _make_switch(description="Toggle read-only mode.")
        attrs = entity.extra_state_attributes
        assert "description" in attrs
        assert attrs["description"] == "Toggle read-only mode."

    def test_uses_attr_is_on(self) -> None:
        """State must live in _attr_is_on, not a custom private field."""
        entity = _make_switch(is_on=True)
        assert hasattr(entity, "_attr_is_on")
        assert entity._attr_is_on is True

    def test_name_via_attr(self) -> None:
        """Name must be set via _attr_name."""
        entity = _make_switch(name="Read Only")
        assert entity._attr_name == "Read Only"


# ===========================================================================
# Switch platform setup
# ===========================================================================


class TestSwitchPlatformSetup:
    """async_setup_entry must register exactly the expected switches."""

    @pytest.mark.asyncio
    async def test_setup_creates_all_switches(self) -> None:
        from custom_components.hsem.switch import SWITCH_DESCRIPTIONS, async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list[HSEMSwitch] = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, False),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        assert len(added) == len(SWITCH_DESCRIPTIONS)
        assert len(added) > 0

    @pytest.mark.asyncio
    async def test_setup_all_entities_are_switch_entities(self) -> None:
        from custom_components.hsem.switch import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, False),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        for entity in added:
            assert isinstance(entity, HSEMSwitch)
            assert isinstance(entity, SwitchEntity)

    @pytest.mark.asyncio
    async def test_setup_unique_ids_are_unique(self) -> None:
        """No two switches may share the same unique_id."""
        from custom_components.hsem.switch import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list[HSEMSwitch] = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, False),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        unique_ids = [e.unique_id for e in added]
        assert len(unique_ids) == len(set(unique_ids)), "Duplicate unique_ids detected"

    @pytest.mark.asyncio
    async def test_setup_reads_initial_state_from_config(self) -> None:
        """Each switch's initial is_on state matches the config entry option."""
        from custom_components.hsem.switch import async_setup_entry

        hass = _mock_hass()
        opts = {
            "hsem_read_only": True,
            "hsem_extended_attributes": False,
            "hsem_verbose_logging": True,
            "hsem_batteries_enable_batteries_schedule_1": False,
            "hsem_batteries_enable_batteries_schedule_2": True,
            "hsem_batteries_enable_batteries_schedule_3": False,
            "hsem_ev_charger_force_max_discharge_power": True,
        }
        config_entry = _mock_config_entry(**opts)
        added: list[HSEMSwitch] = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, False),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        by_key = {e.entity_description.key: e for e in added}
        for key, expected in opts.items():
            assert by_key[key].is_on == expected, f"{key}: expected {expected}"


# ===========================================================================
# Time unique_id stability
# ===========================================================================


class TestTimeUniqueId:
    """Time unique_id must preserve the existing format to avoid breaking registries."""

    def test_unique_id_format(self) -> None:
        """unique_id must be 'hsem_<key>_time'."""
        key = "hsem_batteries_enable_batteries_schedule_1_start"
        entity = _make_time(key=key)
        assert entity.unique_id == f"{DOMAIN}_{key}_time"

    def test_unique_id_all_times(self) -> None:
        """Every time key produces the expected 'hsem_<key>_time' id."""
        from custom_components.hsem.time import TIME_DESCRIPTIONS

        for desc in TIME_DESCRIPTIONS:
            entity = _make_time(key=desc.key, name=str(desc.name))
            assert entity.unique_id == f"{DOMAIN}_{desc.key}_time"

    def test_unique_id_is_attr_not_property(self) -> None:
        entity = _make_time()
        assert hasattr(entity, "_attr_unique_id")
        assert entity._attr_unique_id == entity.unique_id

    def test_different_keys_produce_different_unique_ids(self) -> None:
        e1 = _make_time(key="hsem_batteries_enable_batteries_schedule_1_start")
        e2 = _make_time(key="hsem_batteries_enable_batteries_schedule_1_end")
        assert e1.unique_id != e2.unique_id


# ===========================================================================
# Time platform setup
# ===========================================================================


class TestTimePlatformSetupExtended:
    """Additional async_setup_entry tests for the time platform."""

    @pytest.mark.asyncio
    async def test_setup_unique_ids_are_unique(self) -> None:
        from custom_components.hsem.time import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list[HSEMTimeEntity] = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, "00:00:00"),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        unique_ids = [e.unique_id for e in added]
        assert len(unique_ids) == len(set(unique_ids)), "Duplicate unique_ids detected"

    @pytest.mark.asyncio
    async def test_setup_reads_initial_value_from_config(self) -> None:
        """Each time entity's native_value matches the persisted config option."""
        from custom_components.hsem.time import async_setup_entry

        hass = _mock_hass()
        opts = {
            "hsem_batteries_enable_batteries_schedule_1_start": "06:30:00",
            "hsem_batteries_enable_batteries_schedule_1_end": "09:00:00",
            "hsem_batteries_enable_batteries_schedule_2_start": "16:00:00",
            "hsem_batteries_enable_batteries_schedule_2_end": "20:30:00",
            "hsem_batteries_enable_batteries_schedule_3_start": "22:00:00",
            "hsem_batteries_enable_batteries_schedule_3_end": "01:00:00",
        }
        config_entry = _mock_config_entry(**opts)
        added: list[HSEMTimeEntity] = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, "00:00:00"),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        by_key = {e.entity_description.key: e for e in added}
        assert by_key[
            "hsem_batteries_enable_batteries_schedule_1_start"
        ].native_value == time(6, 30)
        assert by_key[
            "hsem_batteries_enable_batteries_schedule_1_end"
        ].native_value == time(9, 0)
        assert by_key[
            "hsem_batteries_enable_batteries_schedule_2_start"
        ].native_value == time(16, 0)
        assert by_key[
            "hsem_batteries_enable_batteries_schedule_2_end"
        ].native_value == time(20, 30)
        assert by_key[
            "hsem_batteries_enable_batteries_schedule_3_start"
        ].native_value == time(22, 0)
        assert by_key[
            "hsem_batteries_enable_batteries_schedule_3_end"
        ].native_value == time(1, 0)

    @pytest.mark.asyncio
    async def test_setup_uses_entity_description(self) -> None:
        """Every time entity exposes entity_description with the correct key."""
        from custom_components.hsem.time import TIME_DESCRIPTIONS, async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list[HSEMTimeEntity] = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        with patch(
            "custom_components.hsem.utils.misc.get_config_value",
            side_effect=lambda entry, key: entry.options.get(key, "00:00:00"),
        ):
            await async_setup_entry(hass, config_entry, add_entities)

        desc_keys = {d.key for d in TIME_DESCRIPTIONS}
        entity_keys = {e.entity_description.key for e in added}
        assert entity_keys == desc_keys


# ===========================================================================
# Select platform setup
# ===========================================================================


class TestSelectPlatformSetup:
    """async_setup_entry must register exactly two selector entities."""

    @pytest.mark.asyncio
    async def test_setup_creates_two_selectors(self) -> None:
        from custom_components.hsem.select import (
            SELECTOR_DESCRIPTIONS,
            async_setup_entry,
        )

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, config_entry, add_entities)

        assert len(added) == len(SELECTOR_DESCRIPTIONS)
        assert len(added) == 2

    @pytest.mark.asyncio
    async def test_setup_entity_is_select_entity(self) -> None:
        from custom_components.hsem.select import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, config_entry, add_entities)

        for entity in added:
            assert isinstance(entity, SelectEntity)

    @pytest.mark.asyncio
    async def test_setup_default_option_is_auto(self) -> None:
        from custom_components.hsem.select import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, config_entry, add_entities)

        # First selector is the working-mode selector (default = "auto").
        assert added[0].current_option == "auto"

    @pytest.mark.asyncio
    async def test_setup_options_include_auto(self) -> None:
        from custom_components.hsem.select import async_setup_entry

        hass = _mock_hass()
        config_entry = _mock_config_entry()
        added: list = []

        def add_entities(entities: list, _update_before_add: bool = False) -> None:
            added.extend(entities)

        await async_setup_entry(hass, config_entry, add_entities)

        # First selector is the working-mode selector (includes "auto").
        assert "auto" in added[0].options


# ===========================================================================
# Selector unique_id stability
# ===========================================================================


class TestSelectorUniqueId:
    """Selector unique_id must use the key directly (hsem_ prefix convention)."""

    def test_unique_id_is_key(self) -> None:
        """unique_id must equal the description key exactly."""
        entity = _make_selector(key="hsem_force_working_mode")
        assert entity.unique_id == "hsem_force_working_mode"

    def test_unique_id_includes_hsem_prefix(self) -> None:
        entity = _make_selector(key="hsem_force_working_mode")
        assert entity.unique_id.startswith("hsem_")

    def test_unique_id_equals_canonical_key(self) -> None:
        """unique_id must equal the canonical key from sensornames, not a caller-supplied key."""
        from custom_components.hsem.utils.sensornames import (
            get_force_working_mode_selector_key,
        )

        entity = _make_selector(key="hsem_force_working_mode")
        assert entity.unique_id == get_force_working_mode_selector_key()

    def test_unique_id_is_attr_not_property(self) -> None:
        entity = _make_selector()
        assert hasattr(entity, "_attr_unique_id")
        assert entity._attr_unique_id == entity.unique_id

    def test_unique_id_format(self) -> None:
        """unique_id must be the key string (e.g. 'hsem_force_working_mode')."""
        entity = _make_selector(key="hsem_force_working_mode")
        assert entity.unique_id == "hsem_force_working_mode"


# ===========================================================================
# Selector behavior
# ===========================================================================


class TestSelectorBehavior:
    """HSEMWorkingModeSelector option selection and validation."""

    @pytest.mark.asyncio
    async def test_select_valid_option(self) -> None:
        entity = _make_selector(options=["auto", "mode_a", "mode_b"])
        entity.async_write_ha_state = MagicMock()
        await entity.async_select_option("mode_a")
        assert entity.current_option == "mode_a"

    @pytest.mark.asyncio
    async def test_select_invalid_option_raises(self) -> None:
        entity = _make_selector(options=["auto", "mode_a"])
        entity.async_write_ha_state = MagicMock()
        with pytest.raises(ValueError):
            await entity.async_select_option("not_a_valid_option")

    @pytest.mark.asyncio
    async def test_select_option_calls_write_ha_state(self) -> None:
        entity = _make_selector(options=["auto", "mode_x"])
        entity.async_write_ha_state = MagicMock()
        await entity.async_select_option("mode_x")
        entity.async_write_ha_state.assert_called_once()

    def test_initial_option_is_default(self) -> None:
        entity = _make_selector(default="auto")
        assert entity.current_option == "auto"

    def test_uses_entity_description(self) -> None:
        entity = _make_selector(
            key="hsem_force_working_mode", name="Force Working Mode"
        )
        assert entity.entity_description is not None
        assert entity.entity_description.key == "hsem_force_working_mode"

    def test_options_match_description(self) -> None:
        opts = ["auto", "BatteriesChargeGrid", "ForceBatteriesDischarge"]
        entity = _make_selector(options=opts)
        assert entity.options == opts


# ===========================================================================
# DeviceInfo — entity.py base class
# ===========================================================================


class TestDeviceInfo:
    """HSEMEntity.device_info must return a DeviceInfo typed dict."""

    def test_device_info_type(self) -> None:
        entity = _make_switch()
        info = entity.device_info
        # DeviceInfo is a TypedDict — it's a plain dict at runtime.
        assert isinstance(info, dict)
        assert "identifiers" in info
        assert "name" in info

    def test_device_info_identifier_contains_domain(self) -> None:
        from custom_components.hsem.const import DOMAIN

        entity = _make_switch()
        identifiers = entity.device_info["identifiers"]
        domains = {t[0] for t in identifiers}
        assert DOMAIN in domains

    def test_device_info_identifier_contains_entry_id(self) -> None:
        entity = _make_switch()
        identifiers = entity.device_info["identifiers"]
        entry_ids = {t[1] for t in identifiers}
        assert entity._config_entry.entry_id in entry_ids


# ===========================================================================
# No property overrides — _attr_* pattern enforcement
# ===========================================================================


class TestAttrPatternEnforcement:
    """Entity platform classes must not re-define @property for attributes that
    already have an _attr_* equivalent in the HA base class."""

    def test_hsem_switch_no_is_on_property(self) -> None:
        """HSEMSwitch must NOT define a @property 'is_on'."""
        prop = HSEMSwitch.__dict__.get("is_on")
        assert prop is None, "HSEMSwitch must not override is_on as a @property"

    def test_hsem_switch_no_name_property(self) -> None:
        """HSEMSwitch must NOT define a @property 'name'."""
        prop = HSEMSwitch.__dict__.get("name")
        assert prop is None, "HSEMSwitch must not override name as a @property"

    def test_hsem_switch_no_unique_id_property(self) -> None:
        """HSEMSwitch must NOT define a @property 'unique_id'."""
        prop = HSEMSwitch.__dict__.get("unique_id")
        assert prop is None, "HSEMSwitch must not override unique_id as a @property"

    def test_hsem_time_no_unique_id_property(self) -> None:
        """HSEMTimeEntity must NOT define a @property 'unique_id'."""
        prop = HSEMTimeEntity.__dict__.get("unique_id")
        assert prop is None, "HSEMTimeEntity must not override unique_id as a @property"

    def test_hsem_selector_no_unique_id_property(self) -> None:
        """HSEMWorkingModeSelector must NOT define a @property 'unique_id'."""
        prop = HSEMWorkingModeSelector.__dict__.get("unique_id")
        assert prop is None, (
            "HSEMWorkingModeSelector must not override unique_id as a @property"
        )

    def test_hsem_selector_no_current_option_property(self) -> None:
        """HSEMWorkingModeSelector must NOT override current_option as a @property."""
        prop = HSEMWorkingModeSelector.__dict__.get("current_option")
        assert prop is None, (
            "HSEMWorkingModeSelector must not override current_option as a @property"
        )
