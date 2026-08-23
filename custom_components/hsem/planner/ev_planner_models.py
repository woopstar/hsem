"""Public EV planner data structures.

Extracted from ``ev_planner.py`` to satisfy the repository's 30 KB / 1000-line
file limit. Pure move: the dataclasses are unchanged and are re-exported from
``ev_planner`` so existing importers keep working.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE

# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class EVChargingSlot:
    """Per-slot EV charging plan entry.

    Energy semantics
    ----------------
    EV chargers draw AC power from the grid or from solar and deliver a
    fraction of that energy to the EV battery.

    - ``estimated_charged_kwh``: energy **delivered to the EV battery** (DC
      side, post charger-efficiency loss).  This is what advances the EV SoC.
    - ``ac_load_kwh``: AC energy **consumed from the house/grid/PV side**.
      ``ac_load_kwh = estimated_charged_kwh / charger_efficiency``.
      With 90 % efficiency, 10 kWh delivered ⇒ 11.11 kWh AC load.
      This value is injected into ``PlannedSlot.ev_planned_load_kwh`` so that
      net consumption, SoC simulation, and cost calculations all see the true
      grid/PV demand.

    Other attributes:
        start: Timezone-aware start of the slot.
        end: Timezone-aware end of the slot.
        solar_surplus_kwh: Solar surplus (battery-side) used for EV charging.
        import_needed_kwh: Battery-side energy from grid (= estimated_charged_kwh
            − solar_surplus_kwh).
        import_price: Import price for this slot (currency/kWh).
        estimated_cost: Estimated grid cost for EV charging this slot.
    """

    start: datetime
    end: datetime
    estimated_charged_kwh: float = 0.0
    ac_load_kwh: float = 0.0
    solar_surplus_kwh: float = 0.0
    import_needed_kwh: float = 0.0
    import_price: float = 0.0
    estimated_cost: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict for HA sensor attributes."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "estimated_charged_kwh": round(self.estimated_charged_kwh, 3),
            "ac_load_kwh": round(self.ac_load_kwh, 3),
            "solar_surplus_kwh": round(self.solar_surplus_kwh, 3),
            "import_needed_kwh": round(self.import_needed_kwh, 3),
            "import_price": round(self.import_price, 4),
            "estimated_cost": round(self.estimated_cost, 4),
        }


@dataclass
class EVPlannerInput:
    """All inputs required to compute an EV charging plan.

    Attributes:
        enabled: Whether EV planned load integration is active.
        ev_connected: True when a vehicle is physically plugged in.
        smart_charging_enabled: True when smart charging is permitted.
        current_soc_pct: Vehicle battery SoC in percent (0–100).
        target_soc_pct: Target SoC in percent (0–100).
        battery_capacity_kwh: EV battery nameplate capacity in kWh.
        charger_power_kw: Charger output power in kW.
        charger_efficiency_pct: Charger efficiency as a percentage (0–100).
        charger_min_power_w: Minimum AC power (W) the charger needs to start.
            Below this the charger will not operate. Default 1380 W.
        deadline: Timezone-aware datetime by which charging must be complete.
        base_load_includes_ev: True when the house consumption sensor already
            includes EV charging power.  When True, planned EV load must not
            be added to net consumption a second time.
        allow_charge_past_target_soc: When True, the planner may continue
            charging past the target SoC using surplus PV that would otherwise
            be curtailed (e.g. battery full, negative export prices).
            Only applies when the EV has reached target SoC but is below 100 %.
            Charge-past-target is handled exclusively by the MILP.
        now: Timezone-aware current datetime.
    """

    enabled: bool = False
    ev_connected: bool = False
    smart_charging_enabled: bool = True
    current_soc_pct: float = 0.0
    target_soc_pct: float = 80.0
    battery_capacity_kwh: float = 0.0
    charger_power_kw: float = 0.0
    charger_efficiency_pct: float = 100.0
    charger_min_power_w: float = 1380.0
    deadline: datetime | None = None
    base_load_includes_ev: bool = False
    allow_charge_past_target_soc: bool = False
    now: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class EVChargingPlan:
    """Output of the EV charging planner.

    Attributes:
        state: Human-readable state string for the HA sensor.
        ev_connected: Whether the EV is connected.
        current_soc_pct: EV battery SoC at plan time.
        target_soc_pct: Target SoC.
        battery_capacity_kwh: EV battery capacity.
        charger_power_kw: Charger rated power.
        charger_min_power_w: Minimum AC power (W) the charger needs to start.
        total_kwh_needed: Total energy needed to reach target.
        deadline: Planning deadline.
        charging_slots: Selected slots with per-slot details.
        planned_load_by_slot: Mapping of slot-start ISO string → planned kWh.
        current_slot_planned_load_kwh: Load allocated to the current slot.
        data_quality: Structured diagnostics dict.
    """

    state: str = STATE_UNAVAILABLE
    ev_connected: bool = False
    base_load_includes_ev: bool = False
    current_soc_pct: float = 0.0
    target_soc_pct: float = 80.0
    battery_capacity_kwh: float = 0.0
    charger_power_kw: float = 0.0
    charger_min_power_w: float = 1380.0
    total_kwh_needed: float = 0.0
    deadline: datetime | None = None
    charging_slots: list[EVChargingSlot] = field(default_factory=list)
    planned_load_by_slot: dict[str, float] = field(default_factory=dict)
    current_slot_planned_load_kwh: float = 0.0
    data_quality: dict[str, Any] = field(default_factory=dict)

    def as_attributes(self) -> dict[str, Any]:
        """Serialise to HA sensor attributes dict."""
        return {
            "battery_capacity_kwh": round(self.battery_capacity_kwh, 2),
            "charge_power_kw": round(self.charger_power_kw, 2),
            "current_soc": round(self.current_soc_pct, 1),
            "target_soc": round(self.target_soc_pct, 1),
            "ev_connected": self.ev_connected,
            "base_load_includes_ev": self.base_load_includes_ev,
            "total_kwh_needed": round(self.total_kwh_needed, 3),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "charging_slots": [s.as_dict() for s in self.charging_slots],
            "planned_load_by_slot": {
                k: round(v, 3) for k, v in self.planned_load_by_slot.items()
            },
            "current_slot_planned_load_kwh": round(
                self.current_slot_planned_load_kwh, 3
            ),
            "data_quality": self.data_quality,
        }
