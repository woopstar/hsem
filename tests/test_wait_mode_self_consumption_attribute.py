"""Tests for the ``wait_mode_self_consumption`` attribute (issue #1255).

With ``self_consumption_with_reserve`` the applier runs a
``batteries_wait_mode`` slot as ``MaximizeSelfConsumption`` above the reserve,
while the plan books no discharge on the slot.  The working-mode sensor says
so through one helper shared with the applier:

- ``wait_mode_self_consumption_surplus_kwh`` returns ``None`` whenever the
  reserve behaviour does not apply, and the surplus otherwise.
- The published ``active`` flag agrees with what the applier writes.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier import async_apply_battery_settings
from custom_components.hsem.custom_sensors.applier_caps import (
    wait_mode_self_consumption_surplus_kwh,
)
from custom_components.hsem.custom_sensors.working_mode_attributes import (
    build_working_mode_attributes,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes
from tests.test_batteries_wait_mode import (
    _LOGGER_PATCH,
    _cfg,
    _live,
    _sensor,
    _wait_rec,
    _write_and_verify_ok,
)

_INACTIVE = {"active": False, "reserve_kwh": None, "surplus_kwh": None}


def _attribute(
    cfg: SensorConfig,
    live: LiveState,
    rec: HourlyRecommendation | None,
    reserve_kwh: float | None,
) -> dict[str, object]:
    """Return the published ``wait_mode_self_consumption`` attribute."""
    data = CoordinatorData(
        cfg=cfg,
        live=live,
        hourly_recommendation=rec,
        current_wait_mode_reserve=reserve_kwh,
    )
    config_entry = MagicMock()
    config_entry.options = {}
    config_entry.data = {}
    attributes = build_working_mode_attributes(
        data,
        cfg,
        live,
        unique_id="uid",
        config_entry=config_entry,
        primary_grid_charge_owned=False,
    )
    attribute: dict[str, object] = attributes["wait_mode_self_consumption"]
    return attribute


class TestSurplusHelper:
    """``wait_mode_self_consumption_surplus_kwh`` branch by branch."""

    def test_surplus_above_reserve(self) -> None:
        """Capacity 2.0 kWh over a 1.0 kWh reserve leaves 1.0 kWh."""
        surplus = wait_mode_self_consumption_surplus_kwh(
            _cfg(), _live(working_mode=WorkingModes.TimeOfUse.value), _wait_rec(), 1.0
        )
        assert surplus == pytest.approx(1.0)

    def test_capacity_below_reserve_is_zero_not_negative(self) -> None:
        """The behaviour applies, but nothing may be used."""
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        live.battery_current_capacity_kwh = 0.4
        surplus = wait_mode_self_consumption_surplus_kwh(_cfg(), live, _wait_rec(), 1.0)
        assert surplus == pytest.approx(0.0)

    def test_strict_behaviour_does_not_apply(self) -> None:
        """Strict Wait holds the battery whatever it stores."""
        cfg = _cfg()
        cfg.batteries_wait_mode_behavior = "strict"
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert (
            wait_mode_self_consumption_surplus_kwh(cfg, live, _wait_rec(), 1.0) is None
        )

    def test_other_label_does_not_apply(self) -> None:
        """Only ``batteries_wait_mode`` is reinterpreted."""
        rec = _wait_rec()
        rec.recommendation = Recommendations.BatteriesDischargeWindowMode.value
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert wait_mode_self_consumption_surplus_kwh(_cfg(), live, rec, 1.0) is None

    def test_missing_reserve_does_not_apply(self) -> None:
        """No reliable reserve falls back to strict Wait."""
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert (
            wait_mode_self_consumption_surplus_kwh(_cfg(), live, _wait_rec(), None)
            is None
        )

    def test_authoritative_held_export_does_not_apply(self) -> None:
        """A held slot with a solved export stays in TOU wait (issue #797)."""
        rec = _wait_rec()
        rec.grid_export_kwh = 0.4
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert wait_mode_self_consumption_surplus_kwh(_cfg(), live, rec, 1.0) is None

    @pytest.mark.parametrize("second", [False, True])
    def test_charging_ev_does_not_apply(self, second: bool) -> None:
        """An active EV keeps its own discharge-cap logic."""
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        (live.ev_second if second else live.ev).is_charging = True
        assert (
            wait_mode_self_consumption_surplus_kwh(_cfg(), live, _wait_rec(), 1.0)
            is None
        )

    def test_planned_ev_does_not_apply(self) -> None:
        """A planned charger command counts before the charger draws power."""
        rec = _wait_rec()
        rec.ev_charger_calculated_power = 3700
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert wait_mode_self_consumption_surplus_kwh(_cfg(), live, rec, 1.0) is None

    def test_idle_charger_standby_power_still_applies(self) -> None:
        """A 4 W standby reading is not a charging EV (issue #1251)."""
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        live.ev.power_w = 4.0
        surplus = wait_mode_self_consumption_surplus_kwh(_cfg(), live, _wait_rec(), 1.0)
        assert surplus == pytest.approx(1.0)


class TestPublishedAttribute:
    """The working-mode sensor's ``wait_mode_self_consumption`` attribute."""

    def test_active_with_reserve_and_surplus(self) -> None:
        """A wait slot above the reserve is published as self-consuming."""
        attribute = _attribute(
            _cfg(), _live(working_mode=WorkingModes.TimeOfUse.value), _wait_rec(), 1.0
        )
        assert attribute["active"] is True
        assert attribute["reserve_kwh"] == pytest.approx(1.0)
        assert attribute["surplus_kwh"] == pytest.approx(1.0)

    def test_at_reserve_is_inactive_but_reports_the_reserve(self) -> None:
        """At the reserve the battery is held; the numbers say why."""
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        live.battery_current_capacity_kwh = 1.0
        attribute = _attribute(_cfg(), live, _wait_rec(), 1.0)
        assert attribute["active"] is False
        assert attribute["reserve_kwh"] == pytest.approx(1.0)
        assert attribute["surplus_kwh"] == pytest.approx(0.0)

    def test_strict_behaviour_is_inactive(self) -> None:
        """Strict Wait publishes no reserve figures."""
        cfg = _cfg()
        cfg.batteries_wait_mode_behavior = "strict"
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert _attribute(cfg, live, _wait_rec(), 1.0) == _INACTIVE

    def test_no_current_recommendation_is_inactive(self) -> None:
        """Before the first plan there is nothing to execute."""
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        assert _attribute(_cfg(), live, None, 1.0) == _INACTIVE

    def test_behaviour_is_published(self) -> None:
        """``batteries_wait_mode_behavior`` is the documented attribute."""
        cfg = _cfg()
        live = _live(working_mode=WorkingModes.TimeOfUse.value)
        data = CoordinatorData(cfg=cfg, live=live)
        config_entry = MagicMock()
        config_entry.options = {}
        config_entry.data = {}
        attributes = build_working_mode_attributes(
            data,
            cfg,
            live,
            unique_id="uid",
            config_entry=config_entry,
            primary_grid_charge_owned=False,
        )
        assert (
            attributes["batteries_wait_mode_behavior"]
            == "self_consumption_with_reserve"
        )


class TestAttributeAgreesWithApplier:
    """``active`` is true exactly when the applier selects self-consumption."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("capacity_kwh", "behaviour", "ev_charging"),
        [
            (2.0, "self_consumption_with_reserve", False),
            (1.0, "self_consumption_with_reserve", False),
            (2.0, "strict", False),
            (2.0, "self_consumption_with_reserve", True),
        ],
    )
    async def test_active_matches_written_working_mode(
        self, capacity_kwh: float, behaviour: str, ev_charging: bool
    ) -> None:
        """The flag and the written working mode come from one decision."""
        sensor = _sensor()
        cfg = _cfg()
        cfg.batteries_wait_mode_behavior = behaviour
        # Start from the opposite mode so the applier always writes one.
        live = _live(working_mode="unknown")
        live.battery_current_capacity_kwh = capacity_kwh
        live.ev.is_charging = ev_charging
        rec = _wait_rec()
        rec.end = rec.start + timedelta(hours=1)

        active = _attribute(cfg, live, rec, 1.0)["active"]

        with (
            patch(_LOGGER_PATCH, new_callable=MagicMock),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_write_and_verify",
                side_effect=_write_and_verify_ok,
            ),
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_select_option",
                new_callable=AsyncMock,
            ) as mock_select,
            patch(
                "custom_components.hsem.custom_sensors.applier.async_set_number_value",
                new_callable=AsyncMock,
            ) as mock_number,
        ):
            await async_apply_battery_settings(
                sensor, cfg, live, rec, 5.0, wait_mode_reserve_kwh=1.0
            )

        written_modes = [
            call.args[2]
            for call in mock_select.await_args_list
            if call.args[1] == "select.wm"
        ]
        written_caps = [
            call.args[2]
            for call in mock_number.await_args_list
            if call.args[1] == "number.maxdis"
        ]
        self_consuming = written_modes == [
            WorkingModes.MaximizeSelfConsumption.value
        ] and (not written_caps or written_caps[-1] > 0)
        assert active is self_consuming
