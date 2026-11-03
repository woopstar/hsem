"""Coordinator load-forecast gaps are published and logged (issue #1110).

A young rolling window can leave an hour block without any stored sample.
Before issue #1110 one such hour held the battery with a bare
``source_unavailable`` and no hint which hour was missing. The coordinator
now publishes the missing and estimated hours in ``data_quality`` and logs
them once per change.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.hourly_data_populator.consumption import (
    ConsumptionPopulation,
)
from custom_components.hsem.ml.history_status import MLHistoryState, MLHistoryStatus
from custom_components.hsem.models.data_quality import DataQuality
from tests.coordinator_fixtures import make_real_coordinator
from tests.test_ha_mock_integration import (
    _BASE_ENTITY_STATES,
    _patch_all_ha_helpers,
    make_fake_config_entry,
    make_fake_hass,
)

_MODULE = "custom_components.hsem.coordinator_load_forecast"
_POPULATE = f"{_MODULE}.populate_avg_house_consumption_from_snapshot"


def _coordinator() -> tuple[HSEMDataUpdateCoordinator, list[CoordinatorData]]:
    coordinator = make_real_coordinator(
        hass=make_fake_hass(dict(_BASE_ENTITY_STATES)),
        config_entry=make_fake_config_entry({"hsem_read_only": True}),
    )
    published: list[CoordinatorData] = []
    coordinator.async_set_updated_data = published.append  # type: ignore[method-assign, assignment]  # test monkey-patch
    return coordinator, published


async def _cycle(
    coordinator: HSEMDataUpdateCoordinator, population: ConsumptionPopulation
) -> MagicMock:
    log = MagicMock()
    with (
        _patch_all_ha_helpers(),
        patch(_POPULATE, return_value=population),
        patch(f"{_MODULE}.async_log", log),
    ):
        await coordinator._async_run_update_cycle()
    return log


def _gap_warnings(log: MagicMock) -> list[tuple]:
    return [
        call.args
        for call in log.call_args_list
        if call.args[0] == "warning" and "No stored consumption sample" in call.args[1]
    ]


class TestPublishedGaps:
    """``data_quality`` names the hours the forecast is missing."""

    @pytest.mark.asyncio
    async def test_estimated_hours_are_published(self) -> None:
        coordinator, published = _coordinator()
        population = ConsumptionPopulation(
            ok=True, missing_hours=(9, 17), estimated_hours=(9, 17)
        )

        await _cycle(coordinator, population)

        quality = published[-1].data_quality.as_dict()
        assert quality["load_forecast_missing_hours"] == [9, 17]
        assert quality["load_forecast_estimated_hours"] == [9, 17]
        assert quality["is_complete"] is False

    @pytest.mark.asyncio
    async def test_too_many_missing_hours_hold_with_hours_listed(self) -> None:
        coordinator, published = _coordinator()
        population = ConsumptionPopulation(ok=False, missing_hours=(1, 2, 3, 4, 5))

        await _cycle(coordinator, population)

        data = published[-1]
        quality = data.data_quality.as_dict()
        assert quality["load_forecast_ready"] is False
        assert quality["load_forecast_reason"] == "source_unavailable"
        assert quality["load_forecast_missing_hours"] == [1, 2, 3, 4, 5]
        assert quality["load_forecast_estimated_hours"] == []
        assert data.plan_explanation.winner_name == "safety_hold"


class TestGapLogging:
    """The gap warning is logged once per change, not every cycle."""

    @pytest.mark.asyncio
    async def test_estimate_warning_is_logged_once(self) -> None:
        coordinator, _published = _coordinator()
        population = ConsumptionPopulation(
            ok=True, missing_hours=(9,), estimated_hours=(9,)
        )

        first = await _cycle(coordinator, population)
        second = await _cycle(coordinator, population)

        assert len(_gap_warnings(first)) == 1
        assert _gap_warnings(first)[0][2] == [9]
        assert _gap_warnings(second) == []

    @pytest.mark.asyncio
    async def test_changed_gaps_are_logged_again(self) -> None:
        coordinator, _published = _coordinator()

        await _cycle(
            coordinator,
            ConsumptionPopulation(ok=True, missing_hours=(9,), estimated_hours=(9,)),
        )
        log = await _cycle(
            coordinator, ConsumptionPopulation(ok=False, missing_hours=tuple(range(6)))
        )

        warnings = _gap_warnings(log)
        assert len(warnings) == 1
        assert warnings[0][2] == list(range(6))

    @pytest.mark.asyncio
    async def test_no_gaps_logs_nothing(self) -> None:
        coordinator, _published = _coordinator()

        log = await _cycle(coordinator, ConsumptionPopulation(ok=True))

        assert _gap_warnings(log) == []


class TestDataQualityGaps:
    """``DataQuality`` treats estimated load hours as incomplete input."""

    def test_estimated_hours_make_report_incomplete(self) -> None:
        assert DataQuality().is_complete is True
        assert DataQuality(load_forecast_missing_hours=[3]).is_complete is True
        assert DataQuality(load_forecast_estimated_hours=[3]).is_complete is False

    def test_as_dict_sorts_hours(self) -> None:
        quality = DataQuality(
            load_forecast_missing_hours=[17, 9],
            load_forecast_estimated_hours=[17, 9],
        ).as_dict()
        assert quality["load_forecast_missing_hours"] == [9, 17]
        assert quality["load_forecast_estimated_hours"] == [9, 17]


# ---------------------------------------------------------------------------
# ML history status (issue #1365)
# ---------------------------------------------------------------------------

_ML_POPULATE = "custom_components.hsem.ml.populator.populate_ml_house_consumption"
_ML_STATUS = "custom_components.hsem.ml.history_status.last_history_status"


def _ml_coordinator() -> HSEMDataUpdateCoordinator:
    coordinator = make_real_coordinator(
        hass=make_fake_hass(dict(_BASE_ENTITY_STATES)),
        config_entry=make_fake_config_entry(
            {"hsem_read_only": True, "hsem_ml_consumption_enabled": True}
        ),
    )
    coordinator.async_set_updated_data = MagicMock()  # type: ignore[method-assign]
    return coordinator


async def _ml_cycle(
    coordinator: HSEMDataUpdateCoordinator, status: MLHistoryStatus, *, ml_ok: bool
) -> MagicMock:
    log = MagicMock()
    with (
        _patch_all_ha_helpers(),
        patch(_POPULATE, return_value=ConsumptionPopulation(ok=True)),
        patch(_ML_POPULATE, AsyncMock(return_value=(ml_ok, None))),
        patch(_ML_STATUS, return_value=status),
        patch(f"{_MODULE}.async_log", log),
    ):
        await coordinator._async_run_update_cycle()
    return log


def _ml_lines(log: MagicMock) -> list[str]:
    return [
        call.args[1] % tuple(call.args[2:])
        for call in log.call_args_list
        if call.args[1].startswith("[ml]")
    ]


class TestMLHistoryStatusLogging:
    """A known wait is logged as a wait, not as a failure."""

    @pytest.mark.asyncio
    async def test_insufficient_history_is_a_skip_with_the_reason(self) -> None:
        coordinator = _ml_coordinator()
        ready_at = datetime(2026, 11, 4, 20, 45, tzinfo=UTC)
        status = MLHistoryStatus(
            MLHistoryState.INSUFFICIENT_HISTORY, 38.6, ready_at, "sensor.e"
        )

        log = await _ml_cycle(coordinator, status, ml_ok=False)

        lines = _ml_lines(log)
        assert len(lines) == 1
        assert "skipped: insufficient_history, 38.6/" in lines[0]
        assert ready_at.isoformat() in lines[0]
        assert "failed" not in lines[0]
        assert coordinator._ml_history_status is status

    @pytest.mark.asyncio
    async def test_other_fallbacks_are_logged_as_failures(self) -> None:
        coordinator = _ml_coordinator()
        status = MLHistoryStatus(MLHistoryState.FAILED, reason="model not trained")

        log = await _ml_cycle(coordinator, status, ml_ok=False)

        lines = _ml_lines(log)
        assert len(lines) == 1
        assert lines[0].startswith(
            "[ml] ML consumption failed (failed, model not trained)"
        )

    @pytest.mark.asyncio
    async def test_success_logs_the_span(self) -> None:
        coordinator = _ml_coordinator()
        status = MLHistoryStatus(MLHistoryState.READY, 41.0)

        log = await _ml_cycle(coordinator, status, ml_ok=True)

        lines = _ml_lines(log)
        assert len(lines) == 1
        assert lines[0].startswith(
            "[ml] ML consumption populated the slots (ready, 41.0/"
        )

    @pytest.mark.asyncio
    async def test_disabled_switch_records_disabled(self) -> None:
        coordinator, _published = _coordinator()

        await _cycle(coordinator, ConsumptionPopulation(ok=True))

        assert coordinator._ml_history_status.state is MLHistoryState.DISABLED
