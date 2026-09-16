"""Tests for the centralized value constants from issue #1025.

Three sets of magic strings previously had no backing constant. These tests pin
each canonical definition and assert every consumer derives from it, so a value
cannot be changed in one place and missed in another.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.hsem.const import (
    DEFAULT_CONFIG_VALUES,
    FORCE_MODE_AUTO,
)
from custom_components.hsem.custom_sensors.force_mode_sensor import (
    FORCE_MODE_SENSOR_OPTIONS,
)
from custom_components.hsem.custom_sensors.ocpp_control import RATE_UNIT_PREF_AUTO
from custom_components.hsem.flows.batteries_wait_mode import (
    WAIT_MODE_BEHAVIOR_OPTIONS,
    validate_batteries_wait_mode_input,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.select import _DEFAULT_OPTION
from custom_components.hsem.utils.recommendations import USER_SELECTABLE_RECS
from custom_components.hsem.utils.wait_mode_behavior import (
    DEFAULT_WAIT_MODE_BEHAVIOR,
    WAIT_MODE_BEHAVIOR_VALUES,
    WaitModeBehavior,
)
from custom_components.hsem.utils.workingmodes import ExcessPvUseInTou

_WAIT_MODE_KEY = "hsem_batteries_wait_mode_behavior"


class TestWaitModeBehavior:
    """The flow's accepted set and the applier's comparison must not diverge."""

    def test_values_are_unchanged(self) -> None:
        """Persisted config values must keep working without a migration."""
        assert WAIT_MODE_BEHAVIOR_VALUES == ("strict", "self_consumption_with_reserve")

    def test_default_is_strict(self) -> None:
        """Strict preserves the pre-#954 behaviour for untouched config entries."""
        assert WaitModeBehavior.Strict.value == DEFAULT_WAIT_MODE_BEHAVIOR

    def test_flow_options_match_canonical_values(self) -> None:
        assert [o["value"] for o in WAIT_MODE_BEHAVIOR_OPTIONS] == list(
            WAIT_MODE_BEHAVIOR_VALUES
        )

    def test_config_entry_default_matches(self) -> None:
        assert DEFAULT_CONFIG_VALUES[_WAIT_MODE_KEY] == DEFAULT_WAIT_MODE_BEHAVIOR

    def test_sensor_config_default_matches(self) -> None:
        assert SensorConfig().batteries_wait_mode_behavior == DEFAULT_WAIT_MODE_BEHAVIOR

    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", WAIT_MODE_BEHAVIOR_VALUES)
    async def test_flow_accepts_every_canonical_value(self, value: str) -> None:
        errors = await validate_batteries_wait_mode_input({_WAIT_MODE_KEY: value})
        assert errors == {}

    @pytest.mark.asyncio
    async def test_flow_rejects_an_unknown_value(self) -> None:
        errors = await validate_batteries_wait_mode_input({_WAIT_MODE_KEY: "nonsense"})
        assert errors == {_WAIT_MODE_KEY: "invalid_wait_mode_behavior"}

    def test_str_enum_compares_equal_to_raw_string(self) -> None:
        """The applier compares cfg (a plain str) against the enum member."""
        assert WaitModeBehavior.SelfConsumptionWithReserve == (
            "self_consumption_with_reserve"
        )


class TestExcessPvUseInTou:
    """Values are Huawei select options and must not be renamed."""

    def test_values_are_the_upstream_option_keys(self) -> None:
        assert ExcessPvUseInTou.Charge.value == "charge"
        assert ExcessPvUseInTou.FedToGrid.value == "fed_to_grid"

    def test_charge_is_not_conflated_with_the_action_label(self) -> None:
        """``ExcessPvUseInTou.Charge`` is an inverter setting, not a direction.

        ``_action_label`` and ``window_hysteresis`` also return ``"charge"``, but
        for a battery *direction*.  The values coincide; the concepts do not, so
        neither may be swapped for the other.
        """
        from custom_components.hsem.utils.prediction_tracker import _action_label

        assert _action_label("batteries_charge_grid") == ExcessPvUseInTou.Charge.value
        assert _action_label("batteries_wait_mode") != ExcessPvUseInTou.Charge.value


class TestForceModeAutoSentinel:
    """ "auto" means "no override" and is defined once."""

    def test_value_is_unchanged(self) -> None:
        assert FORCE_MODE_AUTO == "auto"

    def test_select_default_option_matches(self) -> None:
        assert _DEFAULT_OPTION == FORCE_MODE_AUTO

    def test_force_mode_sensor_lists_it_first(self) -> None:
        assert [FORCE_MODE_AUTO, *USER_SELECTABLE_RECS] == FORCE_MODE_SENSOR_OPTIONS

    def test_sentinel_is_not_a_forceable_mode(self) -> None:
        """It is the absence of an override, so it must not be a Recommendation."""
        assert FORCE_MODE_AUTO not in USER_SELECTABLE_RECS

    def test_ocpp_rate_unit_auto_stays_separate(self) -> None:
        """Same string, unrelated concept — must stay two independent constants.

        ``RATE_UNIT_PREF_AUTO`` is an OCPP charging-rate-unit preference. Its
        value coincides with the force-mode sentinel, which makes it tempting to
        collapse them; that would couple EV charger config to planner overrides.
        """
        assert RATE_UNIT_PREF_AUTO == "auto"
        ocpp_source = (
            Path(__file__).parent.parent
            / "custom_components"
            / "hsem"
            / "custom_sensors"
            / "ocpp_control.py"
        ).read_text(encoding="utf-8")
        assert "FORCE_MODE_AUTO" not in ocpp_source
