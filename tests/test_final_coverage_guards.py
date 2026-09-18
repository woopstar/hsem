"""Final guard-clause tests for #1044.

Three narrow contracts: which EVs the MILP co-optimises (and the fallback to
fixed EV loads when none qualify), the forcible-discharge loop's abort on a
failed write, and the EV-charging sensor's state restore.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from homeassistant.const import STATE_OFF, STATE_ON

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier_forcible_discharge import (
    _async_apply_forcible_discharge,
)
from custom_components.hsem.custom_sensors.ev_charging_sensor import (
    HSEMEVChargingSensor,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity
from custom_components.hsem.models.ev_config import EVConfig
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.planner.milp._ev_net_load import (
    ActiveEvNetLoad,
    resolve_active_evs_and_net_load,
)
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus

_FORCIBLE_MODULE = "custom_components.hsem.custom_sensors.applier_forcible_discharge"
_NET_LOAD_MODULE = "custom_components.hsem.planner.milp._ev_net_load"
_NOW = datetime(2026, 6, 1, 12, 30, tzinfo=UTC)
_SLOT = timedelta(hours=1)


def _slots() -> list[PlannedSlot]:
    """Return two slots with house load and PV."""
    return [
        PlannedSlot(
            start=_NOW.replace(minute=0) + i * _SLOT,
            end=_NOW.replace(minute=0) + (i + 1) * _SLOT,
            avg_house_consumption_kwh=2.0,
            solcast_pv_estimate_kwh=0.5,
            ev_accounted_load_kwh=1.0,
        )
        for i in range(2)
    ]


def _resolve(ev_configs: list[EVConfig] | None) -> ActiveEvNetLoad:
    """Resolve active EVs against a fixed two-slot net load."""
    base = np.asarray([1.5, 1.5], dtype=float)
    return resolve_active_evs_and_net_load(
        ev_configs=ev_configs,
        slots=_slots(),
        future_idx=[0, 1],
        now=_NOW,
        net_load=base,
        pv_avail=np.maximum(-base, 0.0),
        base_load=np.maximum(base, 0.0),
    )


class TestResolveActiveEvs:
    """Only an EV the MILP can actually move is co-optimised."""

    def test_a_chargeable_ev_is_co_optimised_and_rebuilds_net_load(self) -> None:
        """An enabled EV with charge headroom rebuilds the net load."""
        ev = EVConfig(enabled=True, capacity_kwh=60.0, max_charge_per_slot=3.0)

        result = _resolve([ev])

        assert result.active_evs == [ev]
        # House load minus the accounted EV load minus PV.
        assert result.net_load[0] == pytest.approx(0.5)

    @pytest.mark.parametrize(
        "ev",
        [
            pytest.param(
                EVConfig(enabled=False, capacity_kwh=60.0, max_charge_per_slot=3.0),
                id="disabled",
            ),
            pytest.param(
                EVConfig(enabled=True, capacity_kwh=0.0, max_charge_per_slot=3.0),
                id="no_capacity",
            ),
            pytest.param(
                EVConfig(enabled=True, capacity_kwh=60.0, max_charge_per_slot=0.0),
                id="no_charge_headroom_or_session",
            ),
        ],
    )
    def test_an_unusable_ev_falls_back_to_fixed_loads(self, ev: EVConfig) -> None:
        """Without a co-optimisable EV the fixed EV loads stand."""
        with patch(f"{_NET_LOAD_MODULE}.log_planner") as log:
            result = _resolve([ev])

        assert result.active_evs == []
        # The original net load is left untouched.
        assert result.net_load[0] == pytest.approx(1.5)
        assert any(
            "falling back to fixed EV loads" in call.args[1]
            for call in log.call_args_list
            if len(call.args) > 1
        )

    def test_a_live_session_qualifies_without_charge_headroom(self) -> None:
        """A measured live session is co-optimised even at zero headroom."""
        ev = EVConfig(
            enabled=True,
            capacity_kwh=60.0,
            max_charge_per_slot=0.0,
            session_charge_kw=7.4,
        )

        assert _resolve([ev]).active_evs == [ev]

    def test_without_ev_configs_nothing_is_co_optimised(self) -> None:
        """No EV configuration means the MILP solves the battery only."""
        result = _resolve(None)

        assert result.active_evs == []
        assert result.net_load[0] == pytest.approx(1.5)


class TestForcibleDischargeAbort:
    """A failed write stops the remaining batteries in the same pass."""

    @pytest.mark.asyncio
    async def test_a_failed_result_stops_the_loop(self) -> None:
        """The loop's abort contract, exercised via its collaborator.

        The current read-back can only answer "active" or "no reading", so
        ``async_write_and_verify`` never actually returns ``FAILED`` for this
        call site (see issue #1058). Stubbing it keeps the loop's own
        behaviour under test regardless of that.
        """
        cfg = SensorConfig()
        cfg.huawei_solar_batteries_forcible_charge = "sensor.forcible_charge"
        cfg.huawei_solar_device_id_batteries = "battery_1"
        cfg.huawei_solar_device_id_batteries_2 = "battery_2"
        live = LiveState()
        live.battery_usable_capacity_kwh = 10.0
        live.huawei_batteries_end_of_discharge_soc_pct = 15.0
        failed = ApplyResult(
            entity_id="sensor.forcible_charge:battery_1",
            desired=1.0,
            actual=0.0,
            status=ApplyStatus.FAILED,
            attempts=3,
        )

        with (
            patch(f"{_FORCIBLE_MODULE}.async_set_forcible_discharge", AsyncMock()),
            patch(
                f"{_FORCIBLE_MODULE}.async_write_and_verify",
                AsyncMock(return_value=failed),
            ) as write_and_verify,
        ):
            results = await _async_apply_forcible_discharge(
                MagicMock(), cfg, live, 1.0, 5000
            )

        assert [r.status for r in results] == [ApplyStatus.FAILED]
        write_and_verify.assert_awaited_once()


class TestEvChargingSensorRestore:
    """The EV charging sensor restores only on/off."""

    @staticmethod
    def _sensor(data: CoordinatorData | None) -> HSEMEVChargingSensor:
        """Return an EV charging sensor bound to a coordinator."""
        entry = MagicMock()
        entry.entry_id = "test_entry"
        entry.options = {}
        entry.data = {}
        coordinator = MagicMock()
        coordinator.data = data
        coordinator.last_update_success = data is not None
        return HSEMEVChargingSensor(entry, coordinator)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [STATE_ON, STATE_OFF])
    async def test_a_known_state_is_restored(self, state: str) -> None:
        """A previous on/off state survives a restart."""
        sensor = self._sensor(None)
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]  # test stub
            return_value=MagicMock(state=state, attributes={})
        )

        with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()

        assert sensor.state == state

    @pytest.mark.asyncio
    async def test_an_unknown_state_is_not_restored(self) -> None:
        """Anything other than on/off falls back to off."""
        sensor = self._sensor(None)
        sensor.async_get_last_state = AsyncMock(  # type: ignore[method-assign]  # test stub
            return_value=MagicMock(state="charging", attributes={})
        )

        with patch.object(HSEMCoordinatorEntity, "async_added_to_hass", AsyncMock()):
            await sensor.async_added_to_hass()

        assert sensor.state == STATE_OFF
