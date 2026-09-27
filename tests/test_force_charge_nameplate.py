"""Force charge commands the charger's whole-amp nameplate (issue #1112).

6.3.x backport of the ``TestForceChargeNameplateCurrent`` tests from
``tests/test_coordinator_helpers_guards.py`` on ``main``; that module is not
on this line.  The ``three_phase_switchable`` case is dropped because phase
switching is ``main``-only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from homeassistant.config_entries import ConfigEntry

from custom_components.hsem.coordinator_helpers import apply_current_ev_power_override
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


class TestForceChargeNameplateCurrent:
    """Force charge commands the whole-amp nameplate, not raw kW (issue #1112).

    A raw ``11.0 kW x 1000`` ceiling is 40 W below the 16 A three-phase
    nameplate, so whole-amp quantisation floored it to 15 A / 10 350 W.
    """

    def _run(
        self,
        options: dict[str, Any],
        *,
        override_primary: bool = True,
        override_second: bool = False,
        house_w: float = 0.0,
    ) -> Any:
        slot = _rec(_SLOT_START, _SLOT_END)
        slot.ev_charger_calculated_power = 0.0
        slot.ev_second_charger_calculated_power = 0.0
        live = LiveState()
        live.ev.is_connected = True
        live.ev_second.is_connected = True
        live.house_consumption_power_w = house_w
        apply_current_ev_power_override(
            config_entry=_entry({"hsem_main_fuse_amps": 0, **options}),
            hourly_recommendations=[slot],
            ev_plan=None,
            ev_second_plan=None,
            now=_NOW,
            override_primary=override_primary,
            override_second=override_second,
            live=live,
        )
        return slot

    @pytest.mark.parametrize(
        ("topology", "power_kw", "expected_w"),
        [
            ("three_phase_balanced", 11.0, 11_040.0),
            ("single_phase", 3.7, 3_680.0),
        ],
    )
    def test_primary_force_charge_uses_nameplate(
        self, topology: str, power_kw: float, expected_w: float
    ) -> None:
        slot = self._run(
            {
                "hsem_ev_planned_load_charger_power_kw": power_kw,
                "hsem_ev_planned_load_charger_phase_topology": topology,
            }
        )

        assert slot.ev_charger_calculated_power == pytest.approx(expected_w)

    def test_second_ev_force_charge_uses_its_own_nameplate(self) -> None:
        slot = self._run(
            {
                "hsem_ev_second_planned_load_charger_power_kw": 11.0,
                "hsem_ev_second_planned_load_charger_phase_topology": (
                    "three_phase_balanced"
                ),
            },
            override_primary=False,
            override_second=True,
        )

        assert slot.ev_second_charger_calculated_power == pytest.approx(11_040.0)

    def test_main_fuse_still_clamps_below_nameplate(self) -> None:
        """Fuse headroom wins over the nameplate: 3x16 A minus 2 kW house."""
        slot = self._run(
            {
                "hsem_main_fuse_amps": 16,
                "hsem_main_fuse_phases": 3,
                "hsem_ev_planned_load_charger_power_kw": 11.0,
                "hsem_ev_planned_load_charger_phase_topology": "three_phase_balanced",
            },
            house_w=2_000.0,
        )

        assert slot.ev_charger_calculated_power == pytest.approx(
            16 * 3 * 230.0 - 2_000.0
        )
