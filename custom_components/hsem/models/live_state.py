"""Pure-Python dataclass representing a live snapshot of all HA entity states.

This module captures everything that :func:`state_collector.async_collect_live_state`
reads from Home Assistant at the start of each update cycle.  The dataclass carries
**no** Home Assistant imports and can be constructed freely in unit tests.

All numeric fields default to ``None`` rather than ``0.0`` so that callers can
distinguish "entity unavailable" from "entity reported zero".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EVLiveState:
    """Live state snapshot for a single EV charger."""

    is_charging: bool = False
    """True when the charger reports an active charging session."""

    power_w: float | None = None
    """Current charging power in Watts, or None if unavailable."""

    soc_pct: float | None = None
    """Vehicle battery state-of-charge as a percentage, or None if unavailable."""

    soc_target_pct: float | None = None
    """Target SoC configured by the user, or None if unavailable."""

    is_connected: bool | None = None
    """True when a vehicle is physically plugged in, or None if unknown."""

    force_max_discharge_power: bool = False
    """True when the charger is configured to force max discharge power."""

    max_discharge_power_w: int = 0
    """Maximum configured discharge power in Watts."""


@dataclass
class TouPeriodsState:
    """Live state of the Huawei TOU charging/discharging periods entity."""

    raw_state: str | None = None
    """String state of the TOU entity (e.g. ``"active"``)."""

    periods: list[Any] = field(default_factory=list)
    """List of period dicts extracted from the entity attributes (Period 1…10)."""


@dataclass
class LiveState:
    """Complete live snapshot of every HA entity read during one update cycle.

    Populated by :func:`~custom_sensors.state_collector.async_collect_live_state`.
    All HA I/O is isolated to that function; this dataclass is a plain carrier.

    Attributes:
        missing_entities: True when one or more required entities were absent or
            returned an unparseable value.
        missing_entities_list: Human-readable list of which entities were missing.
        force_working_mode: Resolved entity_id of the force-mode select, or None.
        force_working_mode_state: Current value of the force-mode select
            (``"auto"`` when not overriding).

        ev: Live state for the primary EV charger.
        ev_second: Live state for the secondary EV charger.

        house_consumption_power_w: Instantaneous house load in Watts.
        solar_production_power_w: Instantaneous PV production in Watts.
        net_consumption_w: Computed net consumption (house − solar − EV if separate).
        net_consumption_with_ev_w: Net consumption including EV draw.

        huawei_batteries_working_mode: Working mode string (e.g. ``"TimeOfUse"``).
        huawei_batteries_soc_pct: Battery state-of-charge in percent.
        huawei_batteries_end_of_discharge_soc_pct: Configured minimum SoC.
        huawei_batteries_grid_charge_cutoff_soc_pct: Grid charge cutoff SoC.
        huawei_batteries_max_charge_power_w: Maximum charging power in Watts.
        huawei_batteries_max_discharge_power_w: Maximum discharging power in Watts.
        huawei_batteries_rated_capacity_wh: Nameplate capacity in Watt-hours.
        huawei_batteries_excess_pv_use_in_tou: Current excess PV use mode string.
        huawei_inverter_active_power_control: Active power control state string.

        tou_periods: TOU period entity state.

        energi_data_service_import_price: Current spot import price (currency/kWh).
        energi_data_service_export_price: Current spot export price (currency/kWh).

        # Derived battery capacities (computed by state_collector)
        battery_usable_capacity_kwh: Usable kWh (rated minus reserve).
        battery_current_capacity_kwh: Currently available kWh above discharge floor.
        battery_rated_capacity_min_kwh: Reserve kWh that must not be discharged.
    """

    # Validation
    missing_entities: bool = False
    missing_entities_list: list[str] = field(default_factory=list)

    # Force working mode
    force_working_mode: str | None = None
    force_working_mode_state: str = "auto"

    # EV chargers
    ev: EVLiveState = field(default_factory=EVLiveState)
    ev_second: EVLiveState = field(default_factory=EVLiveState)

    # Power readings
    house_consumption_power_w: float = 0.0
    solar_production_power_w: float = 0.0
    net_consumption_w: float = 0.0
    net_consumption_with_ev_w: float = 0.0

    # Huawei Solar battery state
    huawei_batteries_working_mode: str | None = None
    huawei_batteries_soc_pct: float | None = None
    huawei_batteries_end_of_discharge_soc_pct: float = 5.0
    huawei_batteries_grid_charge_cutoff_soc_pct: float | None = None
    huawei_batteries_max_charge_power_w: float | None = None
    huawei_batteries_max_discharge_power_w: float | None = None
    huawei_batteries_rated_capacity_wh: float | None = None
    huawei_batteries_excess_pv_use_in_tou: str | None = None
    huawei_inverter_active_power_control: str | None = None

    # TOU periods
    tou_periods: TouPeriodsState = field(default_factory=TouPeriodsState)

    # Energy prices
    energi_data_service_import_price: float = 0.0
    energi_data_service_export_price: float = 0.0

    # Derived battery capacities (set by state_collector after computing them)
    battery_usable_capacity_kwh: float = 0.0
    battery_current_capacity_kwh: float = 0.0
    battery_rated_capacity_min_kwh: float = 0.0

    @property
    def is_forced_mode(self) -> bool:
        """Return True when a manual working mode override is active."""
        return self.force_working_mode_state != "auto"

    @property
    def any_ev_charging(self) -> bool:
        """Return True if at least one EV charger reports an active session."""
        return self.ev.is_charging or self.ev_second.is_charging

    def add_missing_entity(self, label: str) -> None:
        """Mark an entity as missing and record its label."""
        self.missing_entities = True
        self.missing_entities_list.append(label)
