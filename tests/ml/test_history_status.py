"""The ML history status says why the ML forecast is or is not in use (#1365)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from homeassistant.core import HomeAssistant

from custom_components.hsem.ml import history_status
from custom_components.hsem.ml.history_status import (
    MLHistoryState,
    MLHistoryStatus,
    available_days_from_ready_at,
    history_cache_key,
    insufficient_history_status,
    last_history_status,
    record_history_status,
)
from custom_components.hsem.models.sensor_config import SensorConfig

NOW = datetime(2026, 11, 3, 15, 4, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_status():
    history_status._status_by_key.clear()
    yield
    history_status._status_by_key.clear()


def _cfg(energy_entity: str | None = "sensor.house_energy") -> SensorConfig:
    cfg = SensorConfig()
    cfg.ml_consumption_energy_entity = energy_entity
    cfg.recommendation_interval_minutes = 15
    cfg.ml_consumption_history_days = 40
    return cfg


class TestAvailableDays:
    """The measured span is derived from the reader's ready-at instant."""

    def test_span_is_minimum_minus_time_to_wait(self) -> None:
        ready_at = NOW + timedelta(days=1, hours=6, minutes=41)
        days = available_days_from_ready_at(NOW, ready_at, 40)
        assert days == pytest.approx(40 - 1 - 6 / 24 - 41 / 1440, abs=1e-6)

    def test_span_never_negative(self) -> None:
        assert available_days_from_ready_at(NOW, NOW + timedelta(days=50), 40) == 0.0


class TestInsufficientHistoryStatus:
    """A short span and an empty recorder are reported differently."""

    def test_short_span_reports_days_and_ready_at(self) -> None:
        ready_at = NOW + timedelta(days=1, hours=6)
        status = insufficient_history_status(NOW, ready_at, 40, "sensor.house_energy")
        assert status.state is MLHistoryState.INSUFFICIENT_HISTORY
        assert status.available_history_days == pytest.approx(38.75)
        assert status.ready_at == ready_at
        assert status.reason == "sensor.house_energy"
        assert status.is_waiting_for_history

    def test_no_ready_at_means_recorder_empty(self) -> None:
        status = insufficient_history_status(NOW, None, 40, "sensor.house_energy")
        assert status.state is MLHistoryState.RECORDER_EMPTY
        assert status.available_history_days == pytest.approx(0.0)
        assert status.ready_at is None
        assert status.is_waiting_for_history

    @pytest.mark.parametrize(
        "state",
        [
            MLHistoryState.PENDING,
            MLHistoryState.DISABLED,
            MLHistoryState.READY,
            MLHistoryState.FAILED,
        ],
    )
    def test_other_states_are_not_waits(self, state: MLHistoryState) -> None:
        assert not MLHistoryStatus(state).is_waiting_for_history


class TestAttributesAndDescription:
    """The sensor attributes and the log summary come from one place."""

    def test_attributes_round_days_and_format_ready_at(self) -> None:
        ready_at = NOW + timedelta(days=1)
        status = MLHistoryStatus(
            MLHistoryState.INSUFFICIENT_HISTORY, 38.6249, ready_at, "x"
        )
        assert status.as_attributes() == {
            "ml_status": "insufficient_history",
            "ml_available_history_days": pytest.approx(38.6),
            "ml_history_ready_at": ready_at.isoformat(),
        }

    def test_attributes_are_null_when_nothing_was_measured(self) -> None:
        assert MLHistoryStatus(MLHistoryState.DISABLED).as_attributes() == {
            "ml_status": "disabled",
            "ml_available_history_days": None,
            "ml_history_ready_at": None,
        }

    def test_describe_lists_only_known_parts(self) -> None:
        ready_at = NOW + timedelta(days=1)
        status = MLHistoryStatus(
            MLHistoryState.INSUFFICIENT_HISTORY, 38.6, ready_at, "sensor.e"
        )
        assert status.describe(40) == (
            f"insufficient_history, 38.6/40 days, next check {ready_at.isoformat()}, "
            "sensor.e"
        )
        assert MLHistoryStatus(MLHistoryState.READY, 41.2).describe(40) == (
            "ready, 41.2/40 days"
        )
        assert MLHistoryStatus(MLHistoryState.DISABLED).describe(40) == "disabled"


class TestStatusStore:
    """Statuses are kept per effective configuration."""

    def test_pending_until_a_populate_recorded_something(self) -> None:
        hass = cast(HomeAssistant, object())
        assert last_history_status(hass, _cfg()).state is MLHistoryState.PENDING

    def test_recorded_status_is_returned_for_the_same_configuration(self) -> None:
        hass = cast(HomeAssistant, object())
        cfg = _cfg()
        key = history_cache_key(hass, cfg)
        assert key is not None
        status = MLHistoryStatus(MLHistoryState.READY, 41.0)
        record_history_status(key, status)
        assert last_history_status(hass, cfg) is status

        other = _cfg()
        other.ml_consumption_history_days = 14
        assert last_history_status(hass, other).state is MLHistoryState.PENDING

    def test_no_energy_entity_is_a_failure(self) -> None:
        hass = cast(HomeAssistant, object())
        cfg = _cfg(energy_entity=None)
        cfg.grid_import_energy_entity = None
        assert history_cache_key(hass, cfg) is None
        status = last_history_status(hass, cfg)
        assert status.state is MLHistoryState.FAILED
        assert status.reason == "no energy entity"

    def test_key_falls_back_to_grid_import_entity(self) -> None:
        hass = cast(HomeAssistant, object())
        cfg = _cfg(energy_entity=None)
        cfg.grid_import_energy_entity = "sensor.grid_import"
        cfg.ml_consumption_net_consumption = True
        cfg.grid_export_energy_entity = "sensor.grid_export"
        assert history_cache_key(hass, cfg) == (
            id(hass),
            "sensor.grid_import",
            "sensor.grid_export",
            True,
            15,
            40,
        )
