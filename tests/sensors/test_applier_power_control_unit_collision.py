"""The 100 W export block must never be confused with 100 % (issue #1130).

With no export cap configured, the applier's two export targets are 100 W
(``GRID_EXPORT_LIMIT_WATT``, the negative-price block) and 100 % (unlimited).
The read-back passed to ``async_write_and_verify`` used to drop the unit, so
``"Unlimited"``, ``"Limited to 100%"`` and ``"Limited to 100W"`` all read as
``100``. The pre-flight then returned ``SKIPPED`` in both directions, and a
post-write read-back in the wrong unit counted as ``OK``.

These tests drive the real ``async_write_and_verify``: only the two
``huawei_solar`` service calls are stubbed and the settle sleep is patched.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.const import GRID_EXPORT_LIMIT_WATT
from custom_components.hsem.custom_sensors.applier_power_control import (
    async_apply_inverter_power_control,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.degraded_mode import DegradedMode
from custom_components.hsem.utils.inverter_verify import (
    DEFAULT_MAX_RETRIES,
    ApplyStatus,
    CycleApplySummary,
)

_MODULE = "custom_components.hsem.custom_sensors.applier_power_control"
_VERIFY = "custom_components.hsem.utils.inverter_verify"
_APC_ENTITY = "sensor.inverter_active_power_control"
_DEVICE = "inverter_1"
_NEGATIVE_PRICE = -0.10
_POSITIVE_PRICE = 0.50


def _sensor(state: str) -> MagicMock:
    """Return a sensor whose active power control entity reports *state*."""
    sensor = MagicMock(spec=["hass"])
    sensor.hass.states.get.return_value = MagicMock(state=state)
    return sensor


def _cfg(max_grid_export_power_kw: float = 0.0) -> SensorConfig:
    """Return an inverter config with a real active power control entity."""
    cfg = SensorConfig()
    cfg.read_only = False
    cfg.export_electricity_min_price = 0.0
    cfg.max_grid_export_power_kw = max_grid_export_power_kw
    cfg.huawei_solar_device_id_inverter_1 = _DEVICE
    cfg.huawei_solar_inverter_active_power_control = _APC_ENTITY
    return cfg


def _live(state: str, export_price: float) -> LiveState:
    """Return a live snapshot with the inverter at *state*."""
    live = LiveState()
    live._degraded_mode = DegradedMode.OK
    live.export_electricity_price = export_price
    live.huawei_inverter_active_power_control = state
    return live


def _inverter_reports(sensor: MagicMock, state: str | None) -> Any:
    """Return a service stub after which the inverter reports *state*.

    ``None`` leaves the reported state unchanged, i.e. the inverter ignored
    the write.
    """

    async def _write(*_args: Any) -> None:
        if state is not None:
            sensor.hass.states.get.return_value.state = state

    return _write


async def _apply(
    sensor: MagicMock,
    live: LiveState,
    *,
    cfg: SensorConfig | None = None,
    after_pct_write: str | None = None,
    after_watt_write: str | None = None,
) -> tuple[CycleApplySummary, AsyncMock, AsyncMock]:
    """Run the applier with only the two service calls stubbed."""
    pct = AsyncMock(side_effect=_inverter_reports(sensor, after_pct_write))
    watt = AsyncMock(side_effect=_inverter_reports(sensor, after_watt_write))
    with (
        patch(f"{_MODULE}.async_set_grid_export_power_pct", pct),
        patch(f"{_MODULE}.async_set_grid_export_power_watt", watt),
        patch(f"{_VERIFY}.asyncio.sleep", AsyncMock()),
    ):
        summary = await async_apply_inverter_power_control(sensor, cfg or _cfg(), live)
    return summary, pct, watt


class TestExportBlockEngages:
    """Negative price, no cap: the 100 W block must be written from 100 %."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start", ["Unlimited", "Limited to 100%"])
    async def test_the_block_is_written_and_verified(self, start: str) -> None:
        sensor = _sensor(start)

        summary, pct, watt = await _apply(
            sensor,
            _live(start, _NEGATIVE_PRICE),
            after_watt_write="Limited to 100W",
        )

        watt.assert_awaited_once_with(sensor, _DEVICE, GRID_EXPORT_LIMIT_WATT)
        pct.assert_not_awaited()
        assert [r.status for r in summary.results] == [ApplyStatus.OK]
        assert summary.results[0].desired == "100w"
        assert summary.results[0].actual == "100w"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("start", ["Unlimited", "Limited to 100%"])
    async def test_a_percent_read_back_does_not_verify_the_block(
        self, start: str
    ) -> None:
        """The inverter ignores the write; 100 % must not confirm 100 W."""
        sensor = _sensor(start)

        summary, _pct, watt = await _apply(sensor, _live(start, _NEGATIVE_PRICE))

        assert watt.await_count == DEFAULT_MAX_RETRIES
        assert [r.status for r in summary.results] == [ApplyStatus.FAILED]
        assert summary.results[0].actual == "100%"


class TestExportBlockLifts:
    """Non-negative price, no cap: 100 W must be lifted back to 100 %."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("read_back", ["Limited to 100%", "Unlimited"])
    async def test_the_block_is_lifted_and_verified(self, read_back: str) -> None:
        sensor = _sensor("Limited to 100W")

        summary, pct, watt = await _apply(
            sensor,
            _live("Limited to 100W", _POSITIVE_PRICE),
            after_pct_write=read_back,
        )

        pct.assert_awaited_once_with(sensor, _DEVICE, 100)
        watt.assert_not_awaited()
        assert [r.status for r in summary.results] == [ApplyStatus.OK]
        assert summary.results[0].desired == "100%"
        assert summary.results[0].actual == "100%"

    @pytest.mark.asyncio
    async def test_a_watt_read_back_does_not_verify_the_lift(self) -> None:
        """The inverter ignores the write; 100 W must not confirm 100 %."""
        sensor = _sensor("Limited to 100W")

        summary, pct, _watt = await _apply(
            sensor, _live("Limited to 100W", _POSITIVE_PRICE)
        )

        assert pct.await_count == DEFAULT_MAX_RETRIES
        assert [r.status for r in summary.results] == [ApplyStatus.FAILED]
        assert summary.results[0].actual == "100w"


class TestPostWriteVerifyIsUnitAware:
    """A write goes out, then the read-back reports 100 in the wrong unit.

    Starting from 80 % keeps the pre-flight out of the way, so only the
    post-write comparison is exercised.
    """

    @pytest.mark.asyncio
    async def test_a_percent_read_back_after_the_block_is_not_ok(self) -> None:
        sensor = _sensor("Limited to 80%")

        summary, _pct, watt = await _apply(
            sensor,
            _live("Limited to 80%", _NEGATIVE_PRICE),
            after_watt_write="Limited to 100%",
        )

        assert watt.await_count == DEFAULT_MAX_RETRIES
        assert [r.status for r in summary.results] == [ApplyStatus.FAILED]
        assert summary.results[0].actual == "100%"

    @pytest.mark.asyncio
    async def test_a_watt_read_back_after_the_lift_is_not_ok(self) -> None:
        sensor = _sensor("Limited to 80%")

        summary, pct, _watt = await _apply(
            sensor,
            _live("Limited to 80%", _POSITIVE_PRICE),
            after_pct_write="Limited to 100W",
        )

        assert pct.await_count == DEFAULT_MAX_RETRIES
        assert [r.status for r in summary.results] == [ApplyStatus.FAILED]
        assert summary.results[0].actual == "100w"


class TestSameUnitSkipsAreUnchanged:
    """A limit already in place, in the right unit, is still not rewritten."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("state", "export_price"),
        [
            ("Limited to 100W", _NEGATIVE_PRICE),
            ("Unlimited", _POSITIVE_PRICE),
            ("Limited to 100%", _POSITIVE_PRICE),
        ],
    )
    async def test_a_matching_limit_is_not_written(
        self, state: str, export_price: float
    ) -> None:
        summary, pct, watt = await _apply(_sensor(state), _live(state, export_price))

        pct.assert_not_awaited()
        watt.assert_not_awaited()
        assert summary.results == []

    @pytest.mark.asyncio
    async def test_the_pre_flight_skips_when_the_live_state_already_matches(
        self,
    ) -> None:
        """A stale snapshot defers to the pre-flight, which still skips."""
        sensor = _sensor("Limited to 100W")

        summary, pct, watt = await _apply(
            sensor, _live("Limited to 80%", _NEGATIVE_PRICE)
        )

        pct.assert_not_awaited()
        watt.assert_not_awaited()
        assert [r.status for r in summary.results] == [ApplyStatus.SKIPPED]
        assert summary.results[0].actual == "100w"


class TestCappedInstalls:
    """With an export cap, both targets are watts, as before."""

    @pytest.mark.asyncio
    async def test_the_block_replaces_the_cap(self) -> None:
        sensor = _sensor("Limited to 8000W")

        summary, _pct, watt = await _apply(
            sensor,
            _live("Limited to 8000W", _NEGATIVE_PRICE),
            cfg=_cfg(max_grid_export_power_kw=8.0),
            after_watt_write="Limited to 100W",
        )

        watt.assert_awaited_once_with(sensor, _DEVICE, GRID_EXPORT_LIMIT_WATT)
        assert [r.status for r in summary.results] == [ApplyStatus.OK]

    @pytest.mark.asyncio
    async def test_the_cap_replaces_the_block(self) -> None:
        sensor = _sensor("Limited to 100W")

        summary, pct, watt = await _apply(
            sensor,
            _live("Limited to 100W", _POSITIVE_PRICE),
            cfg=_cfg(max_grid_export_power_kw=8.0),
            after_watt_write="Limited to 8000W",
        )

        watt.assert_awaited_once_with(sensor, _DEVICE, 8000)
        pct.assert_not_awaited()
        assert [r.status for r in summary.results] == [ApplyStatus.OK]
        assert summary.results[0].desired == "8000w"

    @pytest.mark.asyncio
    async def test_a_cap_already_in_place_is_not_written(self) -> None:
        summary, pct, watt = await _apply(
            _sensor("Limited to 8000W"),
            _live("Limited to 8000W", _POSITIVE_PRICE),
            cfg=_cfg(max_grid_export_power_kw=8.0),
        )

        pct.assert_not_awaited()
        watt.assert_not_awaited()
        assert summary.results == []
