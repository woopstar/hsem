"""Tests for the ``hsem.what_if`` service (issue #1432).

The service is read-only by construction: it hands the scenario to the
coordinator's replay and returns what comes back.  These tests pin the
translation of service data into scenarios, the errors a caller can get, and
that nothing is ever sent to another service.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.hsem import services as services_module, services_what_if
from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator_what_if import (
    WhatIfBusyError,
    WhatIfUnavailableError,
)
from custom_components.hsem.models.what_if_history import MAX_HISTORY_DAYS
from custom_components.hsem.services import SERVICE_HANDLER_MAP
from custom_components.hsem.services_what_if import (
    SCHEMA_WHAT_IF,
    SERVICE_WHAT_IF,
    async_handle_what_if,
    scenarios_from_call,
)
from custom_components.hsem.utils.schema import vol
from custom_components.hsem.utils.what_if import MAX_SCENARIOS, WhatIfScenario

_SERVICES_YAML = (
    Path(__file__).parent.parent / "custom_components" / "hsem" / "services.yaml"
)
_RESPONSE = {"days_compared": 4, "days_skipped": 1, "scenarios": []}


def _call(hass: MagicMock, data: dict[str, Any]) -> ServiceCall:
    return ServiceCall(
        hass=hass, domain=DOMAIN, service=SERVICE_WHAT_IF, data=SCHEMA_WHAT_IF(data)
    )


@pytest.fixture
def hass() -> MagicMock:
    mock = MagicMock(spec=HomeAssistant)
    mock.services = MagicMock()
    mock.services.async_call = AsyncMock()
    return mock


@pytest.fixture
def coordinator() -> Iterator[MagicMock]:
    mock = MagicMock()
    with patch.object(services_module, "_get_coordinator", return_value=mock):
        yield mock


class TestSchema:
    def test_the_history_defaults_to_thirty_days(self) -> None:
        assert SCHEMA_WHAT_IF({})["history_days"] == 30

    @pytest.mark.parametrize("days", [0, MAX_HISTORY_DAYS + 1])
    def test_the_history_is_bounded_by_what_is_kept(self, days: int) -> None:
        assert SCHEMA_WHAT_IF({"history_days": MAX_HISTORY_DAYS})
        with pytest.raises(vol.Invalid):
            SCHEMA_WHAT_IF({"history_days": days})

    @pytest.mark.parametrize(
        "data",
        [
            {"usable_capacity_kwh": 0.0},
            {"max_charge_power_w": 0.0},
            {"charge_efficiency_pct": 101.0},
            {"discharge_efficiency_pct": 0.0},
            {"cycle_cost_per_kwh": -0.1},
            {"capacity_price_per_kwh": -1.0},
            {"battery_brand": "other"},
        ],
    )
    def test_values_no_battery_can_have_are_refused(self, data: dict[str, Any]) -> None:
        with pytest.raises(vol.Invalid):
            SCHEMA_WHAT_IF(data)

    def test_services_yaml_describes_exactly_the_schema_fields(self) -> None:
        described = yaml.safe_load(_SERVICES_YAML.read_text(encoding="utf-8"))
        fields = described[SERVICE_WHAT_IF]["fields"]

        assert set(fields) == {str(key) for key in SCHEMA_WHAT_IF.schema}
        days = fields["history_days"]["selector"]["number"]
        assert (days["min"], days["max"]) == (1, MAX_HISTORY_DAYS)

    def test_it_is_registered_as_a_response_only_service(self) -> None:
        schema, handler, supports_response = SERVICE_HANDLER_MAP[SERVICE_WHAT_IF]

        assert schema is SCHEMA_WHAT_IF
        assert handler is async_handle_what_if
        assert supports_response is SupportsResponse.ONLY


class TestScenarios:
    def test_the_overrides_become_one_scenario_in_planner_units(self) -> None:
        scenarios = scenarios_from_call(
            SCHEMA_WHAT_IF(
                {
                    "usable_capacity_kwh": 15,
                    "max_charge_power_w": 3000,
                    "max_discharge_power_w": 4000,
                    "charge_efficiency_pct": 95,
                    "discharge_efficiency_pct": 90,
                    "cycle_cost_per_kwh": 0.25,
                }
            )
        )

        assert len(scenarios) == 1
        scenario = scenarios[0]
        assert scenario.usable_kwh == pytest.approx(15.0)
        assert scenario.max_charge_kw == pytest.approx(3.0)
        assert scenario.max_discharge_kw == pytest.approx(4.0)
        assert scenario.charge_efficiency == pytest.approx(0.95)
        assert scenario.discharge_efficiency == pytest.approx(0.90)
        assert scenario.cycle_cost_per_kwh == pytest.approx(0.25)

    def test_what_is_not_given_stays_as_configured(self) -> None:
        scenarios = scenarios_from_call(SCHEMA_WHAT_IF({"cycle_cost_per_kwh": 0}))

        assert scenarios == (WhatIfScenario(cycle_cost_per_kwh=0.0),)

    def test_a_range_gives_one_scenario_per_size_with_the_other_overrides(
        self,
    ) -> None:
        scenarios = scenarios_from_call(
            SCHEMA_WHAT_IF(
                {
                    "capacity_start_kwh": 5,
                    "capacity_end_kwh": 15,
                    "capacity_step_kwh": 5,
                    "max_charge_power_w": 3000,
                    "charge_efficiency_pct": 95,
                    "discharge_efficiency_pct": 90,
                    "max_discharge_power_w": 4000,
                    "cycle_cost_per_kwh": 0.25,
                }
            )
        )

        assert [scenario.usable_kwh for scenario in scenarios] == pytest.approx(
            [5.0, 10.0, 15.0]
        )
        for scenario in scenarios:
            assert scenario.max_charge_kw == pytest.approx(3.0)
            assert scenario.max_discharge_kw == pytest.approx(4.0)
            assert scenario.charge_efficiency == pytest.approx(0.95)
            assert scenario.discharge_efficiency == pytest.approx(0.90)
            assert scenario.cycle_cost_per_kwh == pytest.approx(0.25)

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({}, "at least one battery override"),
            ({"history_days": 7, "capacity_price_per_kwh": 100}, "at least one"),
            ({"capacity_start_kwh": 5, "capacity_end_kwh": 15}, "capacity range needs"),
            (
                {
                    "usable_capacity_kwh": 12,
                    "capacity_start_kwh": 5,
                    "capacity_end_kwh": 15,
                    "capacity_step_kwh": 5,
                },
                "not both",
            ),
            (
                {
                    "capacity_start_kwh": 15,
                    "capacity_end_kwh": 5,
                    "capacity_step_kwh": 5,
                },
                "start <= end",
            ),
            (
                {
                    "capacity_start_kwh": 1,
                    "capacity_end_kwh": MAX_SCENARIOS + 1,
                    "capacity_step_kwh": 1,
                },
                f"at most {MAX_SCENARIOS}",
            ),
        ],
    )
    def test_a_call_that_describes_no_comparison_is_refused(
        self, data: dict[str, Any], message: str
    ) -> None:
        with pytest.raises(ServiceValidationError, match=message):
            scenarios_from_call(SCHEMA_WHAT_IF(data))


class TestHandler:
    @pytest.mark.asyncio
    async def test_no_coordinator_is_a_validation_error(self, hass: MagicMock) -> None:
        with (
            patch.object(services_module, "_get_coordinator", return_value=None),
            pytest.raises(ServiceValidationError, match="coordinator not found"),
        ):
            await async_handle_what_if(_call(hass, {"usable_capacity_kwh": 15}))

    @pytest.mark.asyncio
    async def test_it_returns_the_replay_with_its_duration(
        self, hass: MagicMock, coordinator: MagicMock
    ) -> None:
        run = AsyncMock(return_value=dict(_RESPONSE))

        with patch.object(services_what_if, "async_run_what_if", run):
            response = await async_handle_what_if(
                _call(
                    hass,
                    {
                        "history_days": 14,
                        "usable_capacity_kwh": 15,
                        "capacity_price_per_kwh": 2500,
                    },
                )
            )

        assert response is not None
        assert response["days_compared"] == 4
        assert isinstance(response["duration_seconds"], float)
        assert run.await_args is not None
        assert run.await_args.args[0] is coordinator
        assert run.await_args.args[1].tzinfo is not None
        assert run.await_args.kwargs == {
            "history_days": 14,
            "scenarios": (WhatIfScenario(usable_kwh=15.0),),
            "capacity_price_per_kwh": pytest.approx(2500.0),
        }

    @pytest.mark.asyncio
    async def test_it_calls_no_other_service_and_no_planner_cycle(
        self, hass: MagicMock, coordinator: MagicMock
    ) -> None:
        """Nothing is written: not to the hardware and not to the plan."""
        coordinator._async_handle_update = AsyncMock()
        run = AsyncMock(return_value=dict(_RESPONSE))

        with patch.object(services_what_if, "async_run_what_if", run):
            await async_handle_what_if(_call(hass, {"usable_capacity_kwh": 15}))

        hass.services.async_call.assert_not_called()
        coordinator._async_handle_update.assert_not_called()
        assert run.await_args is not None
        assert run.await_args.kwargs["capacity_price_per_kwh"] is None

    @pytest.mark.asyncio
    async def test_a_second_call_while_one_runs_is_refused(
        self, hass: MagicMock, coordinator: MagicMock
    ) -> None:
        run = AsyncMock(side_effect=WhatIfBusyError("busy"))

        with (
            patch.object(services_what_if, "async_run_what_if", run),
            pytest.raises(ServiceValidationError, match="already running"),
        ):
            await async_handle_what_if(_call(hass, {"usable_capacity_kwh": 15}))

    @pytest.mark.asyncio
    async def test_before_the_first_plan_it_says_so(
        self, hass: MagicMock, coordinator: MagicMock
    ) -> None:
        run = AsyncMock(side_effect=WhatIfUnavailableError("no planner cycle yet"))

        with (
            patch.object(services_what_if, "async_run_what_if", run),
            pytest.raises(HomeAssistantError, match="no planner cycle yet"),
        ):
            await async_handle_what_if(_call(hass, {"usable_capacity_kwh": 15}))

    @pytest.mark.asyncio
    async def test_a_cancelled_run_is_an_error_not_an_empty_answer(
        self, hass: MagicMock, coordinator: MagicMock
    ) -> None:
        run = AsyncMock(return_value=None)

        with (
            patch.object(services_what_if, "async_run_what_if", run),
            pytest.raises(HomeAssistantError, match="cancelled"),
        ):
            await async_handle_what_if(_call(hass, {"usable_capacity_kwh": 15}))

    @pytest.mark.asyncio
    async def test_a_bad_scenario_is_refused_before_anything_runs(
        self, hass: MagicMock, coordinator: MagicMock
    ) -> None:
        run = AsyncMock()

        with (
            patch.object(services_what_if, "async_run_what_if", run),
            pytest.raises(ServiceValidationError),
        ):
            await async_handle_what_if(_call(hass, {}))

        run.assert_not_awaited()
