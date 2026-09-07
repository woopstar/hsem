"""Regression tests for the PV curtailment sensor's derived heuristic (issue #925).

Since issue #767, ``export_electricity_min_price``/``batteries_export_min_price``
only gate intentional battery-to-grid export — PV surplus export continues
unrestricted at any non-negative export price. The applier only physically
blocks the whole grid connection point when the export price is negative.

The derived curtailment heuristic previously assumed "export price below the
configured minimum" meant "grid export is blocked", which stopped being true
once #767 shipped. That produced a false ``"curtailed"`` reading whenever the
battery was full, PV was producing, the active-power-control register was
unavailable, and the export price was merely low-but-positive — exactly the
scenario reported in issue #925 (export price 0.004 with a 0.02 floor).
"""

from __future__ import annotations

from custom_components.hsem.custom_sensors.pv_curtailment_sensor import (
    _is_derived_curtailment,
    _is_directly_limited,
)
from custom_components.hsem.models.live_state import LiveState


def _live(
    *,
    solar_w: float = 2000.0,
    soc_pct: float | None = 96.0,
    export_price: float = 0.004,
    power_control: str | None = None,
) -> LiveState:
    live = LiveState()
    live.solar_production_power_w = solar_w
    live.huawei_batteries_soc_pct = soc_pct
    live.export_electricity_price = export_price
    live.huawei_inverter_active_power_control = power_control
    return live


class TestDerivedCurtailmentPositivePriceNotBlocked:
    """Issue #925: a low-but-non-negative export price must not read as curtailed."""

    def test_low_positive_price_below_configured_floor_is_not_curtailed(self) -> None:
        # Mirrors the issue report: export_electricity_min_price=0.02,
        # live export price=0.004, battery near-full, PV producing, register
        # unavailable. PV export is not blocked at this price (issue #767),
        # so the sensor must not report "curtailed".
        live = _live(export_price=0.004, soc_pct=96.0, power_control=None)

        assert _is_derived_curtailment(live) is False

    def test_zero_export_price_is_not_curtailed(self) -> None:
        live = _live(export_price=0.0, soc_pct=100.0, power_control=None)

        assert _is_derived_curtailment(live) is False


class TestDerivedCurtailmentNegativePriceStillDetected:
    """Negative export prices are the only case HSEM itself blocks export."""

    def test_negative_price_with_full_battery_is_curtailed(self) -> None:
        live = _live(export_price=-0.01, soc_pct=96.0, power_control=None)

        assert _is_derived_curtailment(live) is True

    def test_negative_price_with_low_soc_is_not_curtailed(self) -> None:
        live = _live(export_price=-0.01, soc_pct=50.0, power_control=None)

        assert _is_derived_curtailment(live) is False

    def test_negative_price_with_no_pv_production_is_not_curtailed(self) -> None:
        live = _live(export_price=-0.01, solar_w=0.0, soc_pct=96.0, power_control=None)

        assert _is_derived_curtailment(live) is False

    def test_negative_price_with_known_unlimited_register_is_not_curtailed(
        self,
    ) -> None:
        # Direct register reading takes precedence over the derived heuristic.
        live = _live(export_price=-0.01, soc_pct=96.0, power_control="Unlimited")

        assert _is_derived_curtailment(live) is False


class TestDirectCurtailmentDetection:
    """Direct register reading is unaffected by this fix."""

    def test_unlimited_is_not_curtailed(self) -> None:
        assert _is_directly_limited("Unlimited") is False

    def test_limited_percentage_is_curtailed(self) -> None:
        assert _is_directly_limited("Limited to 80%") is True

    def test_limited_watt_is_curtailed(self) -> None:
        assert _is_directly_limited("Limited to 100W") is True

    def test_none_is_not_curtailed(self) -> None:
        assert _is_directly_limited(None) is False
