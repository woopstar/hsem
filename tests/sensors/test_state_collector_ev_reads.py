"""Tests for the EV planned-load reads and deadline resolution.

``test_state_collector.py`` covers the main collection pass. These tests
drive the two EV helpers directly: the per-EV planned-load reader (which must
only ever touch its own prefixed fields) and the deadline resolver (entity,
fixed fallback, unusable formats, and the roll-over to tomorrow).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.custom_sensors.state_collector import (
    _read_ev_planned_load_state,
    _resolve_ev_deadline_from_params,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig

_MODULE = "custom_components.hsem.custom_sensors.state_collector"
# ``hsem_now`` is imported inside the resolver, so patch it at its source.
_CLOCK = "custom_components.hsem.utils.datetime_utils.now"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _sensor(options: dict[str, Any] | None = None) -> MagicMock:
    """Return a stand-in sensor exposing a config entry with *options*."""
    sensor = MagicMock()
    sensor._config_entry.options = dict(options or {})
    sensor._config_entry.data = {}
    return sensor


def _reader(values: dict[str, Any]) -> Any:
    """Return a ``_read``-shaped closure resolving *values* by entity id."""

    def _read(
        entity_id: str | None,
        conv_type: str | None = None,
        decimals: int = 3,
        label: str = "",
    ) -> Any:
        return values.get(entity_id or "")

    return _read


class TestReadEvPlannedLoadState:
    """Each EV's planned-load fields are read under its own prefix."""

    @pytest.mark.parametrize(
        ("is_second", "prefix"),
        [(False, "ev_planned_load"), (True, "ev_second_planned_load")],
        ids=["primary", "second"],
    )
    def test_reads_only_its_own_prefixed_fields(
        self, is_second: bool, prefix: str
    ) -> None:
        """Connection, SoC, target, smart charging, and deadline are set."""
        cfg = SensorConfig()
        charger = cfg.ev_second if is_second else cfg.ev
        charger.connected_entity = "binary_sensor.ev_connected"
        charger.soc_entity = "sensor.ev_soc"
        options = {
            "hsem_ev_second_smart_charging"
            if is_second
            else "hsem_ev_smart_charging": (True),
            "hsem_ev_second_target_soc" if is_second else "hsem_ev_target_soc": 90,
            "hsem_ev_second_deadline_time"
            if is_second
            else "hsem_ev_deadline_time": "07:30",
        }
        state = LiveState()

        with patch(_CLOCK, return_value=_NOW):
            _read_ev_planned_load_state(
                _sensor(options),
                state,
                cfg,
                _reader(
                    {
                        "binary_sensor.ev_connected": "on",
                        "sensor.ev_soc": 42.5,
                    }
                ),
                is_second,
            )

        assert getattr(state, f"{prefix}_connected") is True
        assert getattr(state, f"{prefix}_current_soc_pct") == pytest.approx(42.5)
        assert getattr(state, f"{prefix}_target_soc_pct") == pytest.approx(90.0)
        assert getattr(state, f"{prefix}_smart_charging_enabled") is True
        deadline = getattr(state, f"{prefix}_deadline")
        assert deadline is not None
        assert (deadline.hour, deadline.minute) == (7, 30)

        # The other EV's fields are untouched.
        other = "ev_planned_load" if is_second else "ev_second_planned_load"
        assert getattr(state, f"{other}_current_soc_pct") is None

    def test_no_connected_sensor_assumes_the_ev_is_plugged_in(self) -> None:
        """Without a connection sensor the planner still schedules charging."""
        cfg = SensorConfig()
        state = LiveState()

        with patch(_CLOCK, return_value=_NOW):
            _read_ev_planned_load_state(_sensor(), state, cfg, _reader({}), False)

        assert state.ev_planned_load_connected is True

    def test_unavailable_soc_stays_missing(self) -> None:
        """An unreadable SoC is never coerced to 0 % (issue #988)."""
        cfg = SensorConfig()
        cfg.ev.soc_entity = "sensor.ev_soc"
        state = LiveState()

        with patch(_CLOCK, return_value=_NOW):
            _read_ev_planned_load_state(
                _sensor(), state, cfg, _reader({"sensor.ev_soc": None}), False
            )

        assert state.ev_planned_load_current_soc_pct is None

    def test_missing_target_soc_falls_back_to_eighty_percent(self) -> None:
        """An unset target SoC uses the documented 80 % default."""
        cfg = SensorConfig()
        state = LiveState()

        with patch(_CLOCK, return_value=_NOW):
            _read_ev_planned_load_state(
                _sensor({"hsem_ev_target_soc": None}), state, cfg, _reader({}), False
            )

        assert state.ev_planned_load_target_soc_pct == pytest.approx(80.0)


class TestResolveEvDeadline:
    """The deadline comes from an entity when present, else from config."""

    def test_reads_a_time_string_from_an_entity(self) -> None:
        """A plain string entity state is used directly."""
        with (
            patch(
                f"{_MODULE}.ha_get_entity_state_and_convert", return_value="06:15:00"
            ),
            patch(_CLOCK, return_value=_NOW),
        ):
            deadline = _resolve_ev_deadline_from_params(
                _sensor(), "time.ev_deadline", "07:00"
            )

        assert deadline is not None
        assert (deadline.hour, deadline.minute) == (6, 15)
        # 06:15 has already passed at 12:00, so it rolls to tomorrow.
        assert deadline.date() == (_NOW + timedelta(days=1)).date()

    def test_reads_a_time_from_a_state_object(self) -> None:
        """A raw HA ``State`` object is unwrapped to its state string."""
        state_obj = State("time.ev_deadline", "23:45")

        with (
            patch(f"{_MODULE}.ha_get_entity_state_and_convert", return_value=state_obj),
            patch(_CLOCK, return_value=_NOW),
        ):
            deadline = _resolve_ev_deadline_from_params(
                _sensor(), "time.ev_deadline", "07:00"
            )

        assert deadline is not None
        assert (deadline.hour, deadline.minute) == (23, 45)
        # 23:45 is still ahead of 12:00, so it stays today.
        assert deadline.date() == _NOW.date()

    def test_unreadable_entity_falls_back_to_the_configured_time(self) -> None:
        """A failing entity read is logged and the config value is used."""
        with (
            patch(
                f"{_MODULE}.ha_get_entity_state_and_convert",
                side_effect=HomeAssistantError("boom"),
            ),
            patch(_CLOCK, return_value=_NOW),
            patch(f"{_MODULE}._LOGGER") as logger,
        ):
            deadline = _resolve_ev_deadline_from_params(
                _sensor(), "time.ev_deadline", "22:00"
            )

        assert deadline is not None
        assert (deadline.hour, deadline.minute) == (22, 0)
        logger.warning.assert_called_once()

    def test_non_time_entity_value_falls_back(self) -> None:
        """An entity value that is neither a string nor a state is ignored."""
        with (
            patch(f"{_MODULE}.ha_get_entity_state_and_convert", return_value=42),
            patch(_CLOCK, return_value=_NOW),
        ):
            deadline = _resolve_ev_deadline_from_params(
                _sensor(), "time.ev_deadline", "21:00"
            )

        assert deadline is not None
        assert (deadline.hour, deadline.minute) == (21, 0)

    @pytest.mark.parametrize("fixed", ["", None], ids=["empty_string", "missing"])
    def test_missing_config_uses_the_documented_default(
        self, fixed: str | None
    ) -> None:
        """Without any configured time the 07:00 default applies."""
        with patch(_CLOCK, return_value=_NOW):
            deadline = _resolve_ev_deadline_from_params(_sensor(), None, fixed)

        assert deadline is not None
        assert (deadline.hour, deadline.minute) == (7, 0)

    @pytest.mark.parametrize("fixed", ["not a time", "7", "07:5"], ids=lambda v: v)
    def test_unparseable_times_resolve_to_no_deadline(self, fixed: str) -> None:
        """A malformed time never becomes a bogus deadline."""
        with patch(_CLOCK, return_value=_NOW):
            assert _resolve_ev_deadline_from_params(_sensor(), None, fixed) is None
