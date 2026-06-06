"""Dataclass for a planner decision for a single time slot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from custom_components.hsem.utils.prices import SlotPrice


@dataclass
class PlannedSlot:
    """Planner decision for a single time slot.

    This is a pure-Python counterpart to
    :class:`~custom_components.hsem.models.hourly_recommendation.HourlyRecommendation`
    that can be constructed and inspected without Home Assistant.

    Electricity prices are stored as a
    :class:`~custom_components.hsem.utils.prices.SlotPrice` named-tuple on
    :attr:`price`.  Access individual prices via ``slot.price.import_price``
    and ``slot.price.export_price``.

    Attributes:
        start:
            Timezone-aware start of the slot.
        end:
            Timezone-aware end of the slot.
        price:
            Import and export prices for this slot as a :class:`SlotPrice`.
            Both values are in local currency/kWh and may be negative.
        solcast_pv_estimate_kwh:
            Forecast PV production in kWh for this slot.
        avg_house_consumption_kwh:
            Weighted (spike-aware) average house consumption in kWh.
        avg_house_consumption_1d_kwh:
            Raw 1-day average contribution in kWh.
        avg_house_consumption_3d_kwh:
            Raw 3-day average contribution in kWh.
        avg_house_consumption_7d_kwh:
            Raw 7-day average contribution in kWh.
        avg_house_consumption_14d_kwh:
            Raw 14-day average contribution in kWh.
        estimated_net_consumption_kwh:
            ``avg_house_consumption_kwh - solcast_pv_estimate_kwh`` in kWh.
            Negative means solar surplus.
        estimated_cost_currency:
            Estimated grid cost (positive = import cost, negative = export
            revenue) in local currency for this slot.
        estimated_battery_soc_pct:
            Estimated battery state-of-charge (%) at the *end* of the slot.
        estimated_battery_capacity_kwh:
            Estimated remaining usable battery capacity (kWh) at the *end*
            of the slot.
        batteries_charged_kwh:
            Energy scheduled to be charged into the battery during this slot
            (kWh, ≥ 0).  This is the energy *stored* after conversion losses
            are applied.
        batteries_discharged_kwh:
            Energy discharged from the battery during this slot (kWh, ≥ 0).
            Populated by the SoC simulation and clamped to the discharge
            power limit and available capacity.
        grid_import_kwh:
            Energy imported from the grid during this slot (kWh, ≥ 0).
            Equals load minus any battery discharge and PV that cover demand.
        grid_export_kwh:
            Energy exported to the grid during this slot (kWh, ≥ 0).
            Equals surplus PV and battery discharge beyond local load.
        recommendation:
            The ``Recommendations`` enum value chosen for this slot
            (stored as its string value so the output stays framework-free)
            or ``None`` if no decision has been made.
        ev_planned_load_kwh:
            Extra EV AC load that must be added to base house consumption for
            planner math.  Zero when ``base_load_includes_ev`` is True (EV
            load is already captured in ``avg_house_consumption``) or when no
            EV is scheduled to charge in this slot.  Used in the net
            consumption formula::

                estimated_net_consumption_kwh
                    = avg_house_consumption_kwh + ev_planned_load_kwh
                      - solcast_pv_estimate_kwh

        ev_accounted_load_kwh:
            EV AC load that is planned for the slot but is **already
            accounted for** by the house consumption sensor.  Non-zero only
            when ``base_load_includes_ev`` is True.  Must **not** be added
            again to ``estimated_net_consumption``.

        ev_total_planned_load_kwh:
            Total EV AC load planned for this slot, regardless of whether it
            is injected or already accounted for::

                ev_total_planned_load_kwh
                    = ev_planned_load_kwh + ev_accounted_load_kwh

            Use this field for diagnostics, UI display, and the
            ``EVSmartCharging`` recommendation label decision (use
            ``ev_total_planned_load_kwh > 0`` instead of
            ``ev_planned_load_kwh > 0`` to detect *any* planned EV charging).
        ev_charger_calculated_power:
            Target AC power (W) for the primary EV charger during this slot.
            Computed from the EV planner's per-slot energy target and charger
            efficiency.  Zero when no primary EV charging is planned in this
            slot.  The applier can use this to throttle the charger via
            the go-e (or compatible) API instead of running at full speed.
        ev_second_charger_calculated_power:
            Same as ``ev_charger_calculated_power``, but for the second EV.
    """

    start: datetime
    end: datetime
    price: SlotPrice = field(default_factory=lambda: SlotPrice(0.0, 0.0))
    solcast_pv_estimate_kwh: float = 0.0
    avg_house_consumption_kwh: float = 0.0
    avg_house_consumption_1d_kwh: float = 0.0
    avg_house_consumption_3d_kwh: float = 0.0
    avg_house_consumption_7d_kwh: float = 0.0
    avg_house_consumption_14d_kwh: float = 0.0
    ev_planned_load_kwh: float = 0.0
    ev_accounted_load_kwh: float = 0.0
    ev_total_planned_load_kwh: float = 0.0
    ev_charger_calculated_power: float = 0.0
    ev_second_charger_calculated_power: float = 0.0
    estimated_net_consumption_kwh: float = 0.0
    estimated_cost_currency: float = 0.0
    estimated_battery_soc_pct: float = 0.0
    estimated_battery_capacity_kwh: float = 0.0
    batteries_charged_kwh: float = 0.0
    batteries_discharged_kwh: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    recommendation: str | None = None
