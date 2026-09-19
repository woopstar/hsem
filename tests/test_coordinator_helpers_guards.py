"""Guard-clause tests for pure helpers in ``coordinator_helpers``.

Covers the boundary cases the main flows never reach: an unconfigured main
fuse, an EV override with nothing to write into, a disconnected EV, load
signature comparison, and the load-hold argument contract.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from homeassistant.config_entries import ConfigEntry

from custom_components.hsem.coordinator_helpers import (
    LoadForecastSignature,
    apply_current_ev_power_override,
    apply_load_forecast_hold,
    ev_site_power_budget_w,
    load_forecast_signatures_match,
    write_ev_slot_commands,
)
from custom_components.hsem.models.live_state import LiveState
from tests.test_coordinator_tracking_forecast import _rec

_NOW = datetime(2026, 6, 1, 12, 5, tzinfo=UTC)
_SLOT_START = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
_SLOT_END = _SLOT_START + timedelta(minutes=15)


def _entry(options: dict[str, Any] | None = None) -> ConfigEntry:
    """Return a config entry exposing *options* over the packaged defaults."""
    entry = MagicMock()
    entry.options = dict(options or {})
    entry.data = {}
    return cast(ConfigEntry, entry)


class TestEvSitePowerBudget:
    """An unconfigured fuse is not a limit; a configured one is shared."""

    def test_unconfigured_fuse_is_unconstrained(self) -> None:
        """A zero fuse rating means "not configured", so there is no ceiling.

        ``None`` is not equivalent here: ``get_config_value`` resolves it to
        the packaged default rating instead.
        """
        budget = ev_site_power_budget_w(_entry({"hsem_main_fuse_amps": 0}), LiveState())

        assert budget == math.inf

    def test_fixed_site_load_is_subtracted_from_the_fuse(self) -> None:
        """The EV may only use what the rest of the house leaves."""
        live = LiveState()
        live.house_consumption_power_w = 2_300.0

        budget = ev_site_power_budget_w(
            _entry({"hsem_main_fuse_amps": 25, "hsem_main_fuse_phases": 3}), live
        )

        assert budget == pytest.approx(25 * 3 * 230.0 - 2_300.0)


def test_write_ev_slot_commands_preserves_two_ev_mixed_accounting() -> None:
    """Command stability must not collapse per-EV baseline semantics."""
    slot = _rec(_SLOT_START, _SLOT_END)

    write_ev_slot_commands(
        slot,
        primary_w=4_000.0,
        second_w=2_000.0,
        remaining_hours=0.25,
        old_planned_ev_kwh=0.0,
        primary_base_load_includes_ev=False,
        second_base_load_includes_ev=True,
    )

    assert slot.ev_planned_load_kwh == pytest.approx(1.0)
    assert slot.ev_accounted_load_kwh == pytest.approx(0.5)
    assert slot.ev_total_planned_load_kwh == pytest.approx(1.5)
    assert slot.grid_import_kwh == pytest.approx(1.0)


class TestApplyCurrentEvPowerOverride:
    """The override only writes when there is a slot with time left in it."""

    def _slot(self) -> Any:
        """Return one recommendation slot carrying EV commands."""
        slot = _rec(_SLOT_START, _SLOT_END)
        slot.ev_charger_calculated_power = 1_000.0
        slot.ev_second_charger_calculated_power = 500.0
        return slot

    def test_no_override_requested_is_a_noop(self) -> None:
        """Neither EV selected → nothing is touched."""
        slot = self._slot()

        apply_current_ev_power_override(
            config_entry=_entry(),
            hourly_recommendations=[slot],
            ev_plan=None,
            ev_second_plan=None,
            now=_NOW,
            override_primary=False,
            override_second=False,
        )

        assert slot.ev_charger_calculated_power == pytest.approx(1_000.0)

    def test_no_current_slot_is_a_noop(self) -> None:
        """Nothing to override when ``now`` falls outside every slot."""
        slot = self._slot()

        apply_current_ev_power_override(
            config_entry=_entry(),
            hourly_recommendations=[slot],
            ev_plan=None,
            ev_second_plan=None,
            now=_SLOT_END + timedelta(minutes=1),
            override_primary=True,
            override_second=False,
        )

        assert slot.ev_charger_calculated_power == pytest.approx(1_000.0)

    def test_exhausted_slot_is_a_noop(self) -> None:
        """A slot with no time left cannot carry more energy."""
        slot = self._slot()

        apply_current_ev_power_override(
            config_entry=_entry(),
            hourly_recommendations=[slot],
            ev_plan=None,
            ev_second_plan=None,
            now=_SLOT_END,
            override_primary=True,
            override_second=False,
        )

        assert slot.ev_charger_calculated_power == pytest.approx(1_000.0)

    def test_disconnected_ev_is_overridden_to_zero(self) -> None:
        """An unplugged EV gets no power even when the override is requested."""
        slot = self._slot()
        live = LiveState()
        live.ev.is_connected = False
        live.ev_second.is_connected = False

        apply_current_ev_power_override(
            config_entry=_entry(
                {
                    "hsem_main_fuse_amps": 25,
                    "hsem_ev_planned_load_charger_power_kw": 11,
                    "hsem_ev_second_planned_load_charger_power_kw": 11,
                }
            ),
            hourly_recommendations=[slot],
            ev_plan=None,
            ev_second_plan=None,
            now=_NOW,
            override_primary=True,
            override_second=True,
            live=live,
        )

        assert slot.ev_charger_calculated_power == pytest.approx(0.0)
        assert slot.ev_second_charger_calculated_power == pytest.approx(0.0)

    def test_connected_ev_is_raised_to_its_charger_rating(self) -> None:
        """A plugged-in EV is pushed up to its rating within the fuse budget."""
        slot = self._slot()
        live = LiveState()
        live.ev.is_connected = True

        apply_current_ev_power_override(
            config_entry=_entry(
                {
                    "hsem_main_fuse_amps": 25,
                    "hsem_ev_planned_load_charger_power_kw": 11,
                }
            ),
            hourly_recommendations=[slot],
            ev_plan=None,
            ev_second_plan=None,
            now=_NOW,
            override_primary=True,
            override_second=False,
            live=live,
        )

        assert slot.ev_charger_calculated_power == pytest.approx(11_000.0)


_SIGNATURE: LoadForecastSignature = (
    (_SLOT_START.isoformat(), 1.0, 1.0, 1.0, 1.0, 1.0),
    (_SLOT_END.isoformat(), 2.0, 2.0, 2.0, 2.0, 2.0),
)


class TestLoadForecastSignaturesMatch:
    """Signature comparison decides whether the accepted plan still applies."""

    def test_two_missing_signatures_match(self) -> None:
        """Nothing known on either side is not a change."""
        assert load_forecast_signatures_match(None, None) is True

    def test_one_missing_signature_does_not_match(self) -> None:
        """Gaining or losing a forecast is a change."""
        assert load_forecast_signatures_match(_SIGNATURE, None) is False
        assert load_forecast_signatures_match(None, _SIGNATURE) is False

    def test_identical_signatures_match(self) -> None:
        """An unchanged forecast keeps the accepted plan valid."""
        assert load_forecast_signatures_match(_SIGNATURE, _SIGNATURE) is True

    def test_different_length_does_not_match(self) -> None:
        """A different number of future slots is a change."""
        assert load_forecast_signatures_match(_SIGNATURE, _SIGNATURE[:1]) is False

    def test_different_slot_start_does_not_match(self) -> None:
        """Same shape but shifted slots is a change."""
        shifted: LoadForecastSignature = (
            (_SLOT_END.isoformat(), 1.0, 1.0, 1.0, 1.0, 1.0),
            _SIGNATURE[1],
        )

        assert load_forecast_signatures_match(_SIGNATURE, shifted) is False

    def test_negligible_value_drift_still_matches(self) -> None:
        """Sub-epsilon load drift does not invalidate the plan."""
        drifted: LoadForecastSignature = (
            (_SLOT_START.isoformat(), 1.0 + 1e-12, 1.0, 1.0, 1.0, 1.0),
            _SIGNATURE[1],
        )

        assert load_forecast_signatures_match(_SIGNATURE, drifted) is True


class TestApplyLoadForecastHold:
    """The hold needs an explicit readiness answer and a current slot."""

    def test_missing_readiness_argument_is_rejected(self) -> None:
        """Neither readiness alias supplied → programming error."""
        with pytest.raises(TypeError, match="load_forecast_ready is required"):
            apply_load_forecast_hold([], LiveState(), _NOW)

    def test_no_current_slot_returns_none(self) -> None:
        """Nothing to hold when ``now`` falls outside every slot."""
        held = apply_load_forecast_hold(
            [_rec(_SLOT_START, _SLOT_END)],
            LiveState(),
            _SLOT_END + timedelta(minutes=1),
            load_forecast_ready=False,
        )

        assert held is None

    def test_ready_forecast_returns_none(self) -> None:
        """A usable forecast never triggers a hold."""
        held = apply_load_forecast_hold(
            [_rec(_SLOT_START, _SLOT_END)],
            LiveState(),
            _NOW,
            load_forecast_ready=True,
        )

        assert held is None
