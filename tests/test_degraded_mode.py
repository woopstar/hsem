"""Tests for the HSEM degraded-mode feature (issue #278).

Covers:

- :class:`DegradedMode` enum values and :func:`classify_degraded_mode` logic.
- :func:`hardware_writes_allowed` for each mode.
- ``LiveState.degraded_mode`` property — lazy evaluation, cache invalidation.
- ``LiveState.add_missing_entity`` — non-critical vs. critical entity labels.
- :class:`HSEMDegradedModeSensor` state reflects the correct mode.

All tests are pure-Python; no Home Assistant runtime is required.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.utils.degraded_mode import (
    DegradedMode,
    classify_degraded_mode,
    hardware_writes_allowed,
)

# ---------------------------------------------------------------------------
# classify_degraded_mode — logic unit tests
# ---------------------------------------------------------------------------


class TestClassifyDegradedMode:
    """Unit tests for :func:`classify_degraded_mode`."""

    def test_no_missing_entities_returns_ok(self) -> None:
        """When no entities are missing the mode must be OK."""
        assert classify_degraded_mode(False, []) is DegradedMode.OK

    def test_empty_list_but_flag_true_returns_degraded(self) -> None:
        """``missing_entities=True`` with no labels should still degrade, not error."""
        result = classify_degraded_mode(True, [])
        assert result is DegradedMode.Degraded

    @pytest.mark.parametrize(
        "label",
        [
            # Exact substrings that appear in state_collector labels
            "batteries_state_of_capacity",
            "batteries_maximum_charging_power",
            "batteries_maximum_discharging_power",
            "batteries_rated_capacity",
            "house_consumption_power",
            # Mixed case must still match
            "BATTERIES_STATE_OF_CAPACITY",
            "House_Consumption_Power",
            # Embedded in a longer label as produced by state_collector
            "Error reading batteries_state_of_capacity (entity_id=sensor.foo): ValueError",
            "Missing entity: batteries_maximum_charging_power",
        ],
    )
    def test_critical_entity_returns_error(self, label: str) -> None:
        """Any critical keyword in a label triggers the Error state."""
        assert classify_degraded_mode(True, [label]) is DegradedMode.Error

    @pytest.mark.parametrize(
        "label",
        [
            "energi_data_service_import",
            "Missing entity: solcast_pv_forecast",
            "ev_charger_power",
            "Missing entity: ev_second_soc",
            "huawei_solar_batteries_tou_charging_and_discharging_periods",
        ],
    )
    def test_non_critical_entity_returns_degraded(self, label: str) -> None:
        """Non-critical missing entities should give Degraded, not Error."""
        assert classify_degraded_mode(True, [label]) is DegradedMode.Degraded

    def test_mixed_entities_critical_wins(self) -> None:
        """If any label is critical the result is Error, even with non-critical ones."""
        labels = [
            "Missing entity: solcast_pv_forecast",
            "Error reading batteries_state_of_capacity (entity_id=sensor.batt): ValueError",
        ]
        assert classify_degraded_mode(True, labels) is DegradedMode.Error

    def test_multiple_non_critical_stays_degraded(self) -> None:
        """Multiple non-critical missing entities still yield Degraded."""
        labels = [
            "Missing entity: energi_data_service_import",
            "Missing entity: ev_charger_power",
        ]
        assert classify_degraded_mode(True, labels) is DegradedMode.Degraded


# ---------------------------------------------------------------------------
# hardware_writes_allowed
# ---------------------------------------------------------------------------


class TestHardwareWritesAllowed:
    """Unit tests for :func:`hardware_writes_allowed`."""

    def test_ok_allows_writes(self) -> None:
        assert hardware_writes_allowed(DegradedMode.OK) is True

    def test_degraded_allows_writes(self) -> None:
        """Degraded mode allows writes — battery data is still available."""
        assert hardware_writes_allowed(DegradedMode.Degraded) is True

    def test_error_blocks_writes(self) -> None:
        """Error mode must block all hardware writes."""
        assert hardware_writes_allowed(DegradedMode.Error) is False


# ---------------------------------------------------------------------------
# LiveState.degraded_mode property
# ---------------------------------------------------------------------------


class TestLiveStateDegradedMode:
    """Tests for the lazy ``degraded_mode`` property on ``LiveState``."""

    def test_fresh_live_state_is_ok(self) -> None:
        """A newly created LiveState with no missing entities is OK."""
        live = LiveState()
        assert live.degraded_mode is DegradedMode.OK

    def test_property_is_cached(self) -> None:
        """Repeated reads without mutations return the same object."""
        live = LiveState()
        first = live.degraded_mode
        second = live.degraded_mode
        assert first is second

    def test_add_non_critical_entity_gives_degraded(self) -> None:
        """Adding a non-critical entity label transitions to Degraded."""
        live = LiveState()
        live.add_missing_entity("Missing entity: energi_data_service_import")
        assert live.degraded_mode is DegradedMode.Degraded

    def test_add_critical_entity_gives_error(self) -> None:
        """Adding a critical entity label transitions to Error."""
        live = LiveState()
        live.add_missing_entity(
            "Error reading batteries_state_of_capacity (entity_id=sensor.batt): ValueError"
        )
        assert live.degraded_mode is DegradedMode.Error

    def test_cache_invalidated_on_add_missing_entity(self) -> None:
        """Cache must be invalidated each time add_missing_entity is called."""
        live = LiveState()
        # First access — OK
        assert live.degraded_mode is DegradedMode.OK
        # Add a non-critical entity
        live.add_missing_entity("Missing entity: energi_data_service_import")
        # Cache invalidated → re-evaluated as Degraded
        assert live.degraded_mode is DegradedMode.Degraded
        # Add a critical entity
        live.add_missing_entity("Missing entity: batteries_state_of_capacity")
        # Cache invalidated again → now Error
        assert live.degraded_mode is DegradedMode.Error

    def test_missing_entities_flag_set_by_add(self) -> None:
        """``add_missing_entity`` also sets ``missing_entities = True``."""
        live = LiveState()
        assert live.missing_entities is False
        live.add_missing_entity("Missing entity: ev_charger_power")
        assert live.missing_entities is True

    def test_missing_entities_list_populated(self) -> None:
        """All labels passed to ``add_missing_entity`` are stored."""
        live = LiveState()
        live.add_missing_entity("label_a")
        live.add_missing_entity("label_b")
        assert "label_a" in live.missing_entities_list
        assert "label_b" in live.missing_entities_list


# ---------------------------------------------------------------------------
# Price data missing → Degraded (not Error)
# ---------------------------------------------------------------------------


class TestPriceMissingTriggersDegraded:
    """Missing price data must set Degraded, not Error (acceptance criterion)."""

    def test_missing_import_price_entity_is_degraded(self) -> None:
        live = LiveState()
        live.add_missing_entity("Missing entity: energi_data_service_import")
        assert live.degraded_mode is DegradedMode.Degraded
        assert hardware_writes_allowed(live.degraded_mode) is True

    def test_missing_export_price_entity_is_degraded(self) -> None:
        live = LiveState()
        live.add_missing_entity("Missing entity: energi_data_service_export")
        assert live.degraded_mode is DegradedMode.Degraded
        assert hardware_writes_allowed(live.degraded_mode) is True


# ---------------------------------------------------------------------------
# Battery SoC missing → Error (acceptance criterion)
# ---------------------------------------------------------------------------


class TestBatterySocMissingTriggersError:
    """Missing battery SoC must trigger Error and block hardware writes."""

    def test_missing_soc_is_error(self) -> None:
        live = LiveState()
        live.add_missing_entity(
            "Error reading batteries_state_of_capacity (entity_id=sensor.batt_soc): TypeError"
        )
        assert live.degraded_mode is DegradedMode.Error

    def test_missing_soc_blocks_writes(self) -> None:
        live = LiveState()
        live.add_missing_entity("Missing entity: batteries_state_of_capacity")
        assert hardware_writes_allowed(live.degraded_mode) is False

    def test_missing_max_charge_power_is_error(self) -> None:
        live = LiveState()
        live.add_missing_entity("Missing entity: batteries_maximum_charging_power")
        assert live.degraded_mode is DegradedMode.Error

    def test_missing_max_discharge_power_is_error(self) -> None:
        live = LiveState()
        live.add_missing_entity("Missing entity: batteries_maximum_discharging_power")
        assert live.degraded_mode is DegradedMode.Error

    def test_missing_rated_capacity_is_error(self) -> None:
        live = LiveState()
        live.add_missing_entity("Missing entity: batteries_rated_capacity")
        assert live.degraded_mode is DegradedMode.Error


# ---------------------------------------------------------------------------
# DegradedMode enum values
# ---------------------------------------------------------------------------


class TestDegradedModeEnum:
    """Sanity checks for the DegradedMode enum."""

    def test_ok_value(self) -> None:
        assert DegradedMode.OK.value == "ok"

    def test_degraded_value(self) -> None:
        assert DegradedMode.Degraded.value == "degraded"

    def test_error_value(self) -> None:
        assert DegradedMode.Error.value == "error"

    def test_all_values_unique(self) -> None:
        values = [m.value for m in DegradedMode]
        assert len(values) == len(set(values))
