"""Tests for the inverter export-power applier's input guards and abort.

This applier decides whether the grid connection point stays open, so it must
refuse to act on a price it cannot interpret (issue #767) and must stop after
a failed write rather than carrying on to the second inverter with an
unverified limit on the first.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.custom_sensors.applier_power_control import (
    async_apply_inverter_power_control,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import ApplyResult, ApplyStatus

_MODULE = "custom_components.hsem.custom_sensors.applier_power_control"
_APC_ENTITY = "sensor.inverter_active_power_control"


def _sensor(state: str = "Limited to 100W") -> MagicMock:
    """Return a sensor whose active-power-control entity reports *state*."""
    sensor = MagicMock()
    sensor.hass.states.get.return_value = MagicMock(state=state)
    return sensor


def _cfg(**overrides: Any) -> SensorConfig:
    """Return a config wired to two inverters with the control entity set."""
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.export_electricity_min_price = 1.0
    cfg.huawei_solar_device_id_inverter_1 = "inverter_1"
    cfg.huawei_solar_device_id_inverter_2 = "inverter_2"
    cfg.huawei_solar_inverter_active_power_control = _APC_ENTITY
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _live(**overrides: Any) -> LiveState:
    """Return a live snapshot with a usable export price."""
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.export_electricity_price = 2.0
    for key, value in overrides.items():
        setattr(live, key, value)
    return live


class TestPriceInputGuards:
    """An unusable price is never turned into a hardware limit."""

    @pytest.mark.asyncio
    async def test_an_unreadable_export_price_writes_nothing(self) -> None:
        """A missing export price must not decide the connection-point limit."""
        with patch(f"{_MODULE}.async_write_and_verify", AsyncMock()) as write:
            summary = await async_apply_inverter_power_control(
                _sensor(), _cfg(), _live(export_electricity_price=None)
            )

        write.assert_not_awaited()
        assert summary.results == []

    @pytest.mark.asyncio
    async def test_an_unusable_minimum_price_writes_nothing(self) -> None:
        """Without a configured minimum there is no threshold to compare to."""
        cfg = _cfg()
        cfg.export_electricity_min_price = None  # type: ignore[assignment]  # bad config

        with patch(f"{_MODULE}.async_write_and_verify", AsyncMock()) as write:
            summary = await async_apply_inverter_power_control(_sensor(), cfg, _live())

        write.assert_not_awaited()
        assert summary.results == []

    @pytest.mark.asyncio
    async def test_an_unusable_export_fee_is_treated_as_zero(self) -> None:
        """A missing fee is a zero margin, not a reason to stop (issue #925)."""
        cfg = _cfg()
        cfg.export_fee_per_kwh = None  # type: ignore[assignment]  # bad config

        with patch(
            f"{_MODULE}.async_write_and_verify",
            AsyncMock(
                return_value=ApplyResult(
                    entity_id=_APC_ENTITY,
                    desired=100,
                    actual=100,
                    status=ApplyStatus.OK,
                    attempts=1,
                )
            ),
        ) as write:
            summary = await async_apply_inverter_power_control(_sensor(), cfg, _live())

        # The decision still runs, so the export price stands on its own.
        assert write.await_count >= 1
        assert summary.results


class TestFailedWriteAbort:
    """A failed export-limit write blocks the remaining inverters."""

    @pytest.mark.asyncio
    async def test_the_second_inverter_is_not_written_after_a_failure(self) -> None:
        """An unverified limit on inverter 1 stops the cycle there."""
        failed = ApplyResult(
            entity_id=_APC_ENTITY,
            desired=100,
            actual=None,
            status=ApplyStatus.FAILED,
            attempts=3,
        )

        with (
            patch(
                f"{_MODULE}.async_write_and_verify", AsyncMock(return_value=failed)
            ) as write,
            patch(f"{_MODULE}._LOGGER") as logger,
        ):
            summary = await async_apply_inverter_power_control(
                _sensor(), _cfg(), _live()
            )

        write.assert_awaited_once()
        assert [r.status for r in summary.results] == [ApplyStatus.FAILED]
        assert any(
            "Blocking further writes this cycle" in str(call.args[0])
            for call in logger.debug.call_args_list
        )
