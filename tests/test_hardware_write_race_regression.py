"""End-to-end regression test for issue #951.

Reproduces the reported incident: the planner recommends
``batteries_charge_grid``, the applier begins its sequential hardware-write
chain (max discharge power → excess-PV-use → TOU periods → working mode),
and live power fluctuates mid-sequence, firing the routine 10s live-power
replan tick (``async_monitor_live_power`` →
``HSEMWorkingModeSensor._handle_coordinator_update``) while the write-and-
verify settle-wait for an *earlier* write is still in progress.

Before the fix, that routine coordinator push would unconditionally cancel
the in-flight task (``_cancel_update_task`` in ``_handle_coordinator_update``),
stranding the sequence before it reached the working-mode write — the
inverter would stay in whatever mode it started in.

After the fix, the push is coalesced (``_write_phase_active`` /
``_coordinator_update_pending``) instead of cancelling, so the full write
chain completes and the inverter ends up in ``time_of_use_luna2000`` with
the force-charge TOU periods applied.

Only the true hardware boundary (``hass.services.async_call`` and
``hass.states.get``) is faked; every applier function
(``async_apply_inverter_power_control``, ``async_apply_battery_settings``,
the ``async_write_and_verify`` retry/verify loop, the HA helper writers and
state readers) runs for real.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyStatus
from custom_components.hsem.utils.recommendations import Recommendations
from custom_components.hsem.utils.workingmodes import WorkingModes

# ---------------------------------------------------------------------------
# Minimal mutable fake hardware — only the true HA boundary is faked.
# ---------------------------------------------------------------------------

_WORKING_MODE_ENTITY = "select.batteries_working_mode"
_EXCESS_PV_ENTITY = "select.batteries_excess_pv_energy_use_in_tou"
_DISCHARGE_POWER_ENTITY = "number.batteries_maximum_discharging_power"
_TOU_ENTITY = "sensor.batteries_tou_periods"


@dataclass
class _FakeState:
    entity_id: str
    state: str
    attributes: dict[str, Any] = field(default_factory=dict)


class _MutableFakeStates:
    """A tiny, mutable stand-in for ``hass.states`` that writers can update."""

    def __init__(self, initial: dict[str, tuple[str, dict[str, Any]]]) -> None:
        self._states = {
            entity_id: _FakeState(entity_id, str(state), dict(attrs))
            for entity_id, (state, attrs) in initial.items()
        }

    def get(self, entity_id: str) -> _FakeState | None:
        return self._states.get(entity_id)

    def set(
        self, entity_id: str, state: Any, attributes: dict[str, Any] | None = None
    ) -> None:
        self._states[entity_id] = _FakeState(entity_id, str(state), attributes or {})


def _make_fake_hass() -> tuple[MagicMock, _MutableFakeStates]:
    """Build a fake hass whose service calls mutate a tiny fake state store."""
    states = _MutableFakeStates(
        {
            _DISCHARGE_POWER_ENTITY: ("0", {}),
            _EXCESS_PV_ENTITY: ("fed_to_grid", {}),
            _WORKING_MODE_ENTITY: (
                WorkingModes.MaximizeSelfConsumption.value,
                {},
            ),
            _TOU_ENTITY: ("active", {}),
        }
    )

    async def _service_call(
        domain: str, service: str, data: dict[str, Any], **kwargs: Any
    ) -> None:
        if domain == "number" and service == "set_value":
            states.set(data["entity_id"], data["value"])
        elif domain == "select" and service == "select_option":
            states.set(data["entity_id"], data["option"])
        elif domain == "huawei_solar" and service == "set_tou_periods":
            periods = data["periods"].split("\n")
            attrs = {f"Period {i + 1}": p for i, p in enumerate(periods)}
            states.set(_TOU_ENTITY, "active", attrs)

    hass = MagicMock()
    hass.states = states
    hass.services = MagicMock()
    hass.services.async_call = AsyncMock(side_effect=_service_call)
    hass.services.has_service = MagicMock(return_value=True)

    def _fake_create_task(coro: Any, *, name: str | None = None) -> asyncio.Task:
        return asyncio.get_event_loop().create_task(coro, name=name)

    hass.async_create_task = MagicMock(side_effect=_fake_create_task)
    return hass, states


# ---------------------------------------------------------------------------
# Sensor / CoordinatorData construction
# ---------------------------------------------------------------------------


def _make_cfg() -> SensorConfig:
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.huawei_solar_device_id_batteries = "device_1"
    cfg.huawei_solar_batteries_maximum_discharging_power = _DISCHARGE_POWER_ENTITY
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = _EXCESS_PV_ENTITY
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = _TOU_ENTITY
    cfg.huawei_solar_batteries_working_mode = _WORKING_MODE_ENTITY
    # No inverter device IDs configured — async_apply_inverter_power_control
    # is real but issues zero writes (its device loop is empty), keeping the
    # test focused on the battery-settings sequence issue #951 is about.
    return cfg


def _make_live() -> LiveState:
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.export_electricity_price = 0.20
    live.import_electricity_price = 0.30
    live.huawei_batteries_rated_capacity_wh = 10000.0
    live.huawei_batteries_max_discharge_power_w = None
    live.huawei_batteries_excess_pv_use_in_tou = "fed_to_grid"
    live.huawei_batteries_working_mode = WorkingModes.MaximizeSelfConsumption.value
    live.huawei_batteries_forcible_charge_state = None
    return live


def _make_rec() -> HourlyRecommendation:
    now = datetime.now(UTC)
    return HourlyRecommendation(
        start=now,
        end=now + timedelta(minutes=15),
        recommendation=Recommendations.BatteriesChargeGrid.value,
        avg_house_consumption_kwh=0.5,
        avg_house_consumption_1d_kwh=0.5,
        avg_house_consumption_3d_kwh=0.5,
        avg_house_consumption_7d_kwh=0.5,
        avg_house_consumption_14d_kwh=0.5,
        batteries_charged_kwh=2.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=5.0,
        estimated_battery_soc_pct=65.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=2.0,
        import_price=0.30,
        solcast_pv_estimate_kwh=0.0,
    )


def _make_sensor(hass: MagicMock, data: CoordinatorData) -> HSEMWorkingModeSensor:
    config_entry = MagicMock()
    config_entry.entry_id = "test_entry_951"

    coordinator = MagicMock()
    coordinator.data = data
    coordinator.last_update_success = True

    sensor = HSEMWorkingModeSensor(config_entry, coordinator)
    sensor.hass = hass
    # Isolate the test from unrelated HA entity-state-writing plumbing —
    # this test is about the hardware-write sequence, not HA state
    # publication (covered elsewhere).
    sensor.async_write_ha_state = MagicMock()  # type: ignore[method-assign,misc]  # test isolation
    return sensor


class TestBatteriesChargeGridSurvivesLivePowerTickRace:
    """Reproduces issue #951 end-to-end with a mid-sequence coordinator push."""

    @pytest.mark.asyncio
    async def test_working_mode_and_tou_periods_still_applied(self) -> None:
        """A coordinator push mid-write-sequence must not strand the sequence.

        Injects a simulated coordinator push (the routine 10s live-power
        tick) during the settle-wait of an *earlier* write in the chain
        (the excess-PV-use write, call #2 of 4), then asserts the sequence
        still runs to completion: the working-mode write lands as
        ``time_of_use_luna2000`` and the TOU periods match the force-charge
        schedule — exactly the scenario from the reported incident.
        """
        hass, states = _make_fake_hass()
        cfg = _make_cfg()
        live = _make_live()
        rec = _make_rec()
        data = CoordinatorData(cfg=cfg, live=live, hourly_recommendation=rec)
        sensor = _make_sensor(hass, data)

        sleep_calls = 0
        push_injected_while_writing = False

        async def _fake_sleep(_seconds: float, *args: Any, **kwargs: Any) -> None:
            nonlocal sleep_calls, push_injected_while_writing
            sleep_calls += 1
            if sleep_calls == 2:
                # Simulate the live-power tick firing mid-sequence: a
                # routine coordinator push while this write's settle-wait
                # is still in progress.
                push_injected_while_writing = sensor._write_phase_active
                sensor._handle_coordinator_update()

        with patch(
            "custom_components.hsem.utils.inverter_verify.asyncio.sleep",
            new=_fake_sleep,
        ):
            sensor._handle_coordinator_update()
            first_task = sensor._update_task
            assert first_task is not None
            await asyncio.wait_for(first_task, timeout=5.0)

            # Capture the summary from this (first) run immediately — before
            # any further ``await`` gives the event loop a chance to run the
            # coalesced follow-up task, which re-processes the same
            # CoordinatorData object and would otherwise overwrite
            # ``data.apply_summary`` with its own (idempotent no-op) result.
            first_run_summary = data.apply_summary

            # Let the coalesced follow-up task (from the injected push)
            # finish too, so nothing leaks into other tests.
            await asyncio.sleep(0)
            if (
                sensor._update_task is not None
                and sensor._update_task is not first_task
            ):
                await asyncio.wait_for(sensor._update_task, timeout=5.0)

        # The injected push must have actually landed mid-write-phase,
        # otherwise this test would not be exercising the race at all.
        assert push_injected_while_writing is True
        assert sleep_calls >= 4

        # The original in-flight task must not have been cancelled by the
        # mid-sequence push — it ran to completion (issue #951).
        assert not first_task.cancelled()

        # The push was coalesced rather than dropped or left stranded.
        assert sensor._coordinator_update_pending is False

        # The full write chain reached the hardware: working mode ends up
        # in time_of_use_luna2000 with the force-charge TOU schedule.
        working_mode_state = states.get(_WORKING_MODE_ENTITY)
        assert working_mode_state is not None
        assert working_mode_state.state == WorkingModes.TimeOfUse.value

        tou_state = states.get(_TOU_ENTITY)
        assert tou_state is not None
        written_periods = [
            tou_state.attributes[key]
            for key in sorted(tou_state.attributes)
            if key.startswith("Period ")
        ]
        assert written_periods == DEFAULT_HSEM_TOU_MODES_FORCE_CHARGE

        # The apply summary from the original (in-flight, raced) run records
        # the working-mode write as a verified success — not stranded/missing.
        assert first_run_summary is not None
        working_mode_results = [
            r for r in first_run_summary.results if r.entity_id == _WORKING_MODE_ENTITY
        ]
        assert len(working_mode_results) == 1
        assert working_mode_results[0].status == ApplyStatus.OK
        assert working_mode_results[0].actual == WorkingModes.TimeOfUse.value
