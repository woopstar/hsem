"""Tests for the initial configuration-flow schema."""

import pytest

from custom_components.hsem.flows.init import get_init_step_schema


@pytest.mark.asyncio
async def test_planning_horizon_selector_offers_supported_horizons() -> None:
    """Expose every planning horizon supported by the planner.

    72 h was removed in issue #1002: spot prices are never published that
    far ahead, so the horizon tail had no real price data.
    """
    schema = await get_init_step_schema(None)
    selector = next(
        value
        for key, value in schema.schema.items()
        if key.schema == "hsem_recommendation_interval_length"
    )

    assert selector.config["options"] == ["12", "24", "36", "48"]
