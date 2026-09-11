"""Regression tests for issue #951: the applier-status sensor must not
regress to ``pending``/``total_writes: 0`` when a coordinator cycle is
superseded before its own hardware-write task has run.

Background
----------
``HSEMWorkingModeSensor._async_apply_hardware_writes`` mutates whichever
``CoordinatorData`` object it was handed (``data.apply_summary = ...``) at
the very end of the write sequence.  Because a full write sequence can take
longer than the routine 10s live-power replan tick, a newer
``CoordinatorData`` snapshot can already be published by the coordinator
before the in-flight write task finishes and mutates the *older* snapshot.
Without carrying ``apply_summary`` forward, the newest snapshot's field
stays at its dataclass default of ``None``, and
``HSEMApplierStatusSensor`` reports ``pending``/``total_writes: 0`` even
though real writes landed moments earlier.

The fix carries ``apply_summary`` forward from ``self.data`` onto each new
``CoordinatorData`` built in ``coordinator_cycle.py::_async_run_update_cycle``
so that a superseding cycle still reflects the last real write outcome.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from custom_components.hsem.custom_sensors.applier_status_sensor import (
    HSEMApplierStatusSensor,
)
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
)
from tests.test_ha_mock_integration import (
    _BASE_ENTITY_STATES,
    _patch_all_ha_helpers,
    make_bare_coordinator,
    make_fake_config_entry,
    make_fake_hass,
)


def _make_summary(n_results: int = 1) -> CycleApplySummary:
    """Return a real completed apply summary with *n_results* OK writes."""
    return CycleApplySummary(
        results=[
            ApplyResult(
                entity_id=f"select.batteries_working_mode_{i}",
                desired="time_of_use_luna2000",
                actual="time_of_use_luna2000",
                status=ApplyStatus.OK,
                attempts=1,
            )
            for i in range(n_results)
        ]
    )


async def _run_two_cycles_carrying_data(coord):
    """Run ``_async_run_update_cycle`` twice, actually publishing ``coord.data``.

    ``make_bare_coordinator`` mocks ``async_set_updated_data`` as a no-op, so
    the real coordinator behaviour of replacing ``coord.data`` with the new
    snapshot has to be reproduced here for the carry-forward logic (which
    reads ``self.data`` — i.e. the *previous* snapshot) to be exercised.
    """
    captured: list = []

    def _publish(d):
        captured.append(d)
        coord.data = d

    coord.async_set_updated_data = _publish  # type: ignore[method-assign]  # test monkey-patch

    with _patch_all_ha_helpers():
        await coord._async_run_update_cycle()
    with _patch_all_ha_helpers():
        await coord._async_run_update_cycle()

    return captured


class TestApplySummaryCarriedForward:
    """CoordinatorData.apply_summary survives onto a superseding cycle."""

    @pytest.mark.asyncio
    async def test_no_summary_yet_stays_none(self) -> None:
        """Before any write cycle completes, apply_summary is genuinely None."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()  # type: ignore[method-assign]  # test monkey-patch

        captured = await _run_two_cycles_carrying_data(coord)

        assert len(captured) == 2
        assert captured[0].apply_summary is None
        assert captured[1].apply_summary is None

    @pytest.mark.asyncio
    async def test_completed_summary_carried_to_superseding_cycle(self) -> None:
        """A summary set on cycle N's data is visible on cycle N+1's data.

        Reproduces the core of issue #951: the working-mode sensor's write
        task completes against whichever CoordinatorData it captured
        (here, ``captured[0]``) — possibly *after* a newer cycle has already
        published. The carry-forward must still surface it on the newest
        snapshot instead of leaving that snapshot's own field at None.
        """
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()  # type: ignore[method-assign]  # test monkey-patch

        captured: list = []

        def _publish(d):
            captured.append(d)
            coord.data = d

        coord.async_set_updated_data = _publish  # type: ignore[method-assign]  # test monkey-patch

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        # Simulate the working-mode sensor's write task completing against
        # this (first) CoordinatorData snapshot.
        summary = _make_summary(n_results=2)
        captured[0].apply_summary = summary

        # A second, superseding cycle runs before the working-mode sensor's
        # own write task for *this* new cycle has had a chance to run.
        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        assert len(captured) == 2
        assert captured[1] is not captured[0]
        assert captured[1].apply_summary is summary


class TestApplierStatusSensorDoesNotRegress:
    """HSEMApplierStatusSensor reflects the carried-forward summary."""

    @pytest.mark.asyncio
    async def test_state_is_not_pending_after_superseding_cycle(self) -> None:
        """state must reflect the carried-forward summary, not 'pending'."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()  # type: ignore[method-assign]  # test monkey-patch

        captured: list = []

        def _publish(d):
            captured.append(d)
            coord.data = d

        coord.async_set_updated_data = _publish  # type: ignore[method-assign]  # test monkey-patch

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        summary = _make_summary(n_results=1)
        captured[0].apply_summary = summary

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        sensor = HSEMApplierStatusSensor(config_entry, coord)

        assert sensor.state != "pending"
        assert sensor.state == ApplyStatus.OK.value

    @pytest.mark.asyncio
    async def test_total_writes_is_not_zero_after_superseding_cycle(self) -> None:
        """extra_state_attributes must not regress to total_writes: 0."""
        config_entry = make_fake_config_entry({"hsem_read_only": True})
        hass = make_fake_hass(_BASE_ENTITY_STATES)
        coord = make_bare_coordinator(hass=hass, config_entry=config_entry)
        coord._set_update_interval = AsyncMock()  # type: ignore[method-assign]  # test monkey-patch

        captured: list = []

        def _publish(d):
            captured.append(d)
            coord.data = d

        coord.async_set_updated_data = _publish  # type: ignore[method-assign]  # test monkey-patch

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        summary = _make_summary(n_results=3)
        captured[0].apply_summary = summary

        with _patch_all_ha_helpers():
            await coord._async_run_update_cycle()

        sensor = HSEMApplierStatusSensor(config_entry, coord)
        attrs = sensor.extra_state_attributes

        assert attrs["total_writes"] == 3
        assert attrs["failed_entities"] == []
