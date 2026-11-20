"""Shared fixtures for the ML tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.hsem.ml import history_cache


@pytest.fixture(autouse=True)
def long_term_statistics() -> Generator[AsyncMock]:
    """Make the recorder hold no long-term statistics (issue #1433).

    The history cache falls back to hourly statistics when the raw states are
    too short.  The tests here run against a fake ``hass`` without a
    recorder, so the fallback reads nothing unless a test sets
    ``return_value``.  A module that tests the real reader overrides this
    fixture by name.
    """
    history_cache._statistics_failed_at.clear()
    with patch(
        "custom_components.hsem.ml.statistics_reader.StatisticsReader"
        ".read_hourly_energy",
        new_callable=AsyncMock,
        return_value={},
    ) as read_hourly_energy:
        yield read_hourly_energy
    history_cache._statistics_failed_at.clear()
