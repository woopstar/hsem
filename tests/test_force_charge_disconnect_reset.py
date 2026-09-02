"""Tests for issue #900 — auto-disable force-charge-now on EV disconnect.

Covers :func:`reset_force_charge_on_disconnect` in isolation, plus a full
disconnect → reconnect flow through :func:`apply_force_charge_now` to prove
that a reconnect never silently re-arms forced charging.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.coordinator_helpers import (
    apply_force_charge_now,
    reset_force_charge_on_disconnect,
)
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.utils.recommendations import Recommendations


def _make_fake_config_entry(overrides: dict[str, Any] | None = None) -> MagicMock:
    """Build a minimal fake config entry backed by a real, mutable options dict."""
    options = dict(DEFAULT_CONFIG_VALUES)
    if overrides:
        options.update(overrides)
    config_entry = MagicMock()
    config_entry.options = options
    config_entry.data = {}
    return config_entry


def _make_fake_hass() -> MagicMock:
    """Build a fake hass whose ``config_entries.async_update_entry`` actually
    mutates the entry's ``options``, mirroring real Home Assistant behaviour.
    """
    hass = MagicMock()

    def _update_entry(entry: MagicMock, *, options: dict[str, Any]) -> bool:
        entry.options = options
        return True

    hass.config_entries.async_update_entry.side_effect = _update_entry
    return hass


def _make_rec(now: datetime) -> HourlyRecommendation:
    return HourlyRecommendation(
        start=now - timedelta(minutes=5),
        end=now + timedelta(minutes=10),
        avg_house_consumption_kwh=0.0,
        avg_house_consumption_1d_kwh=0.0,
        avg_house_consumption_3d_kwh=0.0,
        avg_house_consumption_7d_kwh=0.0,
        avg_house_consumption_14d_kwh=0.0,
        batteries_charged_kwh=0.0,
        batteries_discharged_kwh=0.0,
        estimated_battery_capacity_kwh=0.0,
        estimated_battery_soc_pct=0.0,
        estimated_cost_currency=0.0,
        estimated_net_consumption_kwh=0.0,
        export_price=0.0,
        grid_export_kwh=0.0,
        grid_import_kwh=0.0,
        import_price=0.0,
        recommendation=Recommendations.BatteriesWaitMode.value,
        solcast_pv_estimate_kwh=0.0,
    )


class TestResetForceChargeOnDisconnect:
    """Unit tests for the standalone reset helper."""

    def test_ev1_disconnect_resets_switch(self) -> None:
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry({"hsem_ev_force_charge_now": True})

        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=True,
            is_connected=False,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )

        assert result is True
        assert config_entry.options["hsem_ev_force_charge_now"] is False
        hass.config_entries.async_update_entry.assert_called_once()

    def test_ev2_disconnect_resets_switch(self) -> None:
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry(
            {"hsem_ev_second_force_charge_now": True}
        )

        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=True,
            is_connected=False,
            option_key="hsem_ev_second_force_charge_now",
            ev_label="EV2",
        )

        assert result is True
        assert config_entry.options["hsem_ev_second_force_charge_now"] is False
        hass.config_entries.async_update_entry.assert_called_once()

    def test_noop_when_already_off(self) -> None:
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry({"hsem_ev_force_charge_now": False})

        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=True,
            is_connected=False,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )

        assert result is False
        hass.config_entries.async_update_entry.assert_not_called()

    def test_noop_when_still_connected(self) -> None:
        """No transition (still connected) must never touch the option."""
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry({"hsem_ev_force_charge_now": True})

        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=True,
            is_connected=True,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )

        assert result is False
        assert config_entry.options["hsem_ev_force_charge_now"] is True
        hass.config_entries.async_update_entry.assert_not_called()

    def test_noop_on_reconnect_transition(self) -> None:
        """A disconnected→connected transition must never turn the switch on."""
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry({"hsem_ev_force_charge_now": False})

        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=False,
            is_connected=True,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )

        assert result is False
        assert config_entry.options["hsem_ev_force_charge_now"] is False
        hass.config_entries.async_update_entry.assert_not_called()

    def test_noop_when_previous_state_unknown(self) -> None:
        """First-plan cycle (``was_connected is None``) must not reset anything."""
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry({"hsem_ev_force_charge_now": True})

        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=None,
            is_connected=False,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )

        assert result is False
        assert config_entry.options["hsem_ev_force_charge_now"] is True
        hass.config_entries.async_update_entry.assert_not_called()


class TestReconnectDoesNotResumeForcedCharging:
    """End-to-end: disconnect resets the switch; reconnecting stays off."""

    def test_reconnect_after_disconnect_leaves_force_charge_off(self) -> None:
        hass = _make_fake_hass()
        config_entry = _make_fake_config_entry({"hsem_ev_force_charge_now": True})

        # Cycle N: EV disconnects mid-session — switch auto-resets.
        reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=True,
            is_connected=False,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )
        assert config_entry.options["hsem_ev_force_charge_now"] is False

        # Cycle N+1: EV reconnects — helper must not flip it back on, since
        # only a user action (the switch itself) may set it True again.
        result = reset_force_charge_on_disconnect(
            hass=hass,
            config_entry=config_entry,
            was_connected=False,
            is_connected=True,
            option_key="hsem_ev_force_charge_now",
            ev_label="EV1",
        )
        assert result is False
        assert config_entry.options["hsem_ev_force_charge_now"] is False

        # And the force-charge-now override itself must be a no-op now that
        # the option is off — no forced full-power charging resumes.
        now = datetime.now(UTC)
        rec = _make_rec(now)
        apply_force_charge_now(
            config_entry=config_entry,
            hourly_recommendations=[rec],
            ev_plan=None,
            ev_second_plan=None,
            now=now,
        )

        assert rec.recommendation == Recommendations.BatteriesWaitMode.value
        assert rec.ev_charger_calculated_power == 0.0
