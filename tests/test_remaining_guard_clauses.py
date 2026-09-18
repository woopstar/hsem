"""Guard-clause tests for the last uncovered branches across HSEM.

Three themes: write-and-verify must report the worst outcome of a cycle and
survive a failing read-back, the dynamic discharge floor must fall back to the
configured minimum whenever it cannot see a future, and the diagnostics dump
must never crash on an object it cannot serialise.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.hsem.models.planned_slot import PlannedSlot
from custom_components.hsem.models.planner_output import PlannerOutput
from custom_components.hsem.utils.diagnostics import (
    _planner_output_summary,
    _serialise_value,
)
from custom_components.hsem.utils.dynamic_floor import DynamicDischargeFloor
from custom_components.hsem.utils.inverter_verify import (
    ApplyResult,
    ApplyStatus,
    CycleApplySummary,
    async_write_and_verify,
)

_VERIFY_MODULE = "custom_components.hsem.utils.inverter_verify"
_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_SLOT = timedelta(hours=1)


def _result(status: ApplyStatus, entity_id: str = "number.x") -> ApplyResult:
    """Return an apply result in *status*."""
    return ApplyResult(
        entity_id=entity_id, desired=1.0, actual=1.0, status=status, attempts=1
    )


class TestCycleApplySummaryStatus:
    """A cycle reports the worst status of its writes."""

    @pytest.mark.parametrize(
        ("statuses", "expected"),
        [
            pytest.param([ApplyStatus.OK], ApplyStatus.OK, id="all_ok"),
            pytest.param(
                [ApplyStatus.OK, ApplyStatus.FAILED], ApplyStatus.FAILED, id="a_failure"
            ),
            pytest.param(
                [ApplyStatus.OK, ApplyStatus.UNVERIFIED],
                ApplyStatus.UNVERIFIED,
                id="an_unverified_write",
            ),
            pytest.param(
                [ApplyStatus.SKIPPED, ApplyStatus.OK],
                ApplyStatus.OK,
                id="ok_beats_skipped",
            ),
            pytest.param([ApplyStatus.SKIPPED], ApplyStatus.SKIPPED, id="all_skipped"),
        ],
    )
    def test_worst_status_wins(
        self, statuses: list[ApplyStatus], expected: ApplyStatus
    ) -> None:
        """The summary is only as good as its worst write."""
        summary = CycleApplySummary(results=[_result(s) for s in statuses])

        assert summary.overall_status is expected

    def test_a_cycle_with_no_writes_is_skipped(self) -> None:
        """Nothing to write is reported as skipped, not as success."""
        assert CycleApplySummary(results=[]).overall_status is ApplyStatus.SKIPPED


class TestWriteAndVerifyReadBackFailure:
    """A read-back that raises is retried, then reported as unverified."""

    @pytest.mark.asyncio
    async def test_a_raising_reader_ends_unverified(self) -> None:
        """The write happened but could never be confirmed."""
        writer = AsyncMock()
        reader = MagicMock(side_effect=RuntimeError("bus timeout"))

        with (
            patch(f"{_VERIFY_MODULE}.asyncio.sleep", AsyncMock()),
            patch(f"{_VERIFY_MODULE}._LOGGER") as logger,
        ):
            result = await async_write_and_verify(
                entity_id="number.batteries_maximum_charging_power",
                desired=5000.0,
                writer=writer,
                reader=reader,
                tolerance=0.0,
                max_retries=2,
            )

        assert result.status is ApplyStatus.UNVERIFIED
        assert result.actual is None
        assert "Read-back error" in (result.error_message or "")
        assert writer.await_count == 2
        assert logger.warning.call_count >= 2

    @pytest.mark.asyncio
    async def test_a_reader_that_recovers_verifies_the_write(self) -> None:
        """A transient read failure does not condemn the write."""
        reader = MagicMock(side_effect=[None, RuntimeError("bus timeout"), 5000.0])

        with patch(f"{_VERIFY_MODULE}.asyncio.sleep", AsyncMock()):
            result = await async_write_and_verify(
                entity_id="number.batteries_maximum_charging_power",
                desired=5000.0,
                writer=AsyncMock(),
                reader=reader,
                tolerance=0.0,
                max_retries=3,
            )

        assert result.status is ApplyStatus.OK


def _floor_slot(
    start: datetime,
    *,
    net_kwh: float = 0.5,
    charged_kwh: float = 0.0,
    recommendation: str | None = None,
) -> PlannedSlot:
    """Return a slot for the bridge-to-refill scan."""
    return PlannedSlot(
        start=start,
        end=start + _SLOT,
        estimated_net_consumption_kwh=net_kwh,
        batteries_charged_kwh=charged_kwh,
        recommendation=recommendation,
    )


class TestDynamicDischargeFloor:
    """Without a visible refill the configured minimum stands."""

    def test_no_slots_uses_the_configured_minimum(self) -> None:
        """An empty horizon cannot justify holding extra reserve."""
        floor, diag = DynamicDischargeFloor().compute_floor(
            now=_NOW, slots=[], usable_kwh=9.0, configured_min_soc_pct=12.0
        )

        assert floor == pytest.approx(12.0)
        assert isinstance(diag, dict)

    def test_only_past_slots_uses_the_configured_minimum(self) -> None:
        """A horizon entirely in the past has no bridge to fund."""
        past = _floor_slot(_NOW - 3 * _SLOT)

        floor, _diag = DynamicDischargeFloor().compute_floor(
            now=_NOW, slots=[past], usable_kwh=9.0, configured_min_soc_pct=12.0
        )

        assert floor == pytest.approx(12.0)

    def test_an_imminent_solar_surplus_needs_no_reserve(self) -> None:
        """A refill in the next slot means nothing has to be bridged."""
        surplus = _floor_slot(_NOW, net_kwh=-2.0)

        floor, diag = DynamicDischargeFloor().compute_floor(
            now=_NOW, slots=[surplus], usable_kwh=9.0, configured_min_soc_pct=12.0
        )

        assert floor == pytest.approx(12.0)
        assert diag.get("refill_type") in {"solar_surplus", "none", None}

    def test_consumption_before_a_solar_refill_raises_the_floor(self) -> None:
        """Energy needed before the refill is reserved above the minimum."""
        slots = [
            _floor_slot(_NOW, net_kwh=1.0),
            _floor_slot(_NOW + _SLOT, net_kwh=1.0),
            _floor_slot(_NOW + 2 * _SLOT, net_kwh=-3.0),
        ]

        floor, diag = DynamicDischargeFloor().compute_floor(
            now=_NOW, slots=slots, usable_kwh=9.0, configured_min_soc_pct=10.0
        )

        assert floor > 10.0
        assert diag["reserve_kwh"] > 0.0

    def test_a_partial_grid_charge_keeps_scanning(self) -> None:
        """Grid charging too small to cover the bridge is counted, not a refill."""
        slots = [
            _floor_slot(_NOW, net_kwh=2.0),
            _floor_slot(
                _NOW + _SLOT,
                net_kwh=0.5,
                charged_kwh=0.2,
                recommendation="batteries_charge_grid",
            ),
            _floor_slot(_NOW + 2 * _SLOT, net_kwh=1.0),
            _floor_slot(_NOW + 3 * _SLOT, net_kwh=-4.0),
        ]

        floor, diag = DynamicDischargeFloor().compute_floor(
            now=_NOW, slots=slots, usable_kwh=9.0, configured_min_soc_pct=10.0
        )

        assert floor > 10.0
        # The scan ran past the partial charge to the solar refill.
        assert diag["bridge_duration_hours"] >= 2.0

    def test_a_planned_grid_charge_counts_as_a_refill(self) -> None:
        """Grid charging covering the bridge ends the scan."""
        slots = [
            _floor_slot(_NOW, net_kwh=1.0),
            _floor_slot(
                _NOW + _SLOT,
                net_kwh=0.5,
                charged_kwh=5.0,
                recommendation="batteries_charge_grid",
            ),
        ]

        floor, diag = DynamicDischargeFloor().compute_floor(
            now=_NOW, slots=slots, usable_kwh=9.0, configured_min_soc_pct=10.0
        )

        assert floor >= 10.0
        assert isinstance(diag, dict)


class TestDiagnosticsSerialisation:
    """The diagnostics dump degrades rather than raising."""

    def test_nested_containers_are_serialised(self) -> None:
        """Lists, tuples, dicts and datetimes all become JSON-safe values."""
        value = {
            "when": _NOW,
            "items": [1, (2, _NOW)],
        }

        serialised = _serialise_value(value)

        assert serialised["when"] == _NOW.isoformat()
        assert serialised["items"][0] == 1
        # A tuple becomes a list so it survives JSON encoding.
        assert serialised["items"][1] == [2, _NOW.isoformat()]

    def test_an_unserialisable_candidate_falls_back_to_its_repr(self) -> None:
        """A candidate that raises while being read is still listed."""

        class _Hostile:
            """A candidate whose attribute access raises."""

            @property
            def name(self) -> str:
                raise RuntimeError("cannot read name")

            def __repr__(self) -> str:
                return "<hostile candidate>"

        summary = _planner_output_summary(PlannerOutput(candidates=[_Hostile()]))

        assert summary["candidates"] == [{"name": "<hostile candidate>"}]

    def test_an_unserialisable_plan_cost_is_reported_as_an_error(self) -> None:
        """A plan cost that cannot be read does not break the dump."""

        class _HostileCost:
            """A cost object whose ``vars()`` view raises."""

            @property
            def __dict__(self) -> dict[str, Any]:  # type: ignore[override]
                raise RuntimeError("cannot read cost")

        output = PlannerOutput()
        output.plan_cost = _HostileCost()  # type: ignore[assignment]  # hostile object

        summary = _planner_output_summary(output)

        assert summary["plan_cost"] == {"error": "could not serialise plan_cost"}

    def test_a_readable_candidate_cost_is_rounded(self) -> None:
        """A numeric candidate cost is rounded for the dump."""
        candidate = MagicMock()
        candidate.name = "milp"
        candidate.is_valid = True
        candidate.rejection_reason = ""
        candidate.cost = 1.23456

        summary = _planner_output_summary(PlannerOutput(candidates=[candidate]))

        assert summary["candidates"][0]["cost"] == pytest.approx(1.2346)
