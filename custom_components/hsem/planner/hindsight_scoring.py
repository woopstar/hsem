"""Score a realized window: cost, regret, potential, savings and capture.

The definitions of issue #1208, shared by the offline backtest
(``tests/backtest/scoring.py``) and the on-device performance sensors
(issue #1182), so that both report the same numbers for the same slots.

Three ways the same window could have gone are compared, over the same slots
and the same prices:

``realized``
    What was paid: grid import × import price − grid export × export price.
``baseline``
    Plain inverter self-consumption — the hardware with nobody controlling it.
``oracle``
    The cheapest the window could have been with perfect foresight and **hard
    limits only**.

**Stored energy at the end of the window is part of the result.**  A run that
ends with a fuller battery has paid for energy it has not used yet, so two
costs are only comparable when both runs start and end at the same stored
energy.  Every number is therefore a difference of two such runs:

``regret = realized − oracle``
    What was left on the table.  The oracle starts where the window started
    and must end with at least the stored energy the battery really ended
    with.  Never negative: the oracle is a lower bound on the realized cost.
``potential = baseline − oracle``
    What the best control could have saved over none.  Both start where the
    window started; the oracle must end where self-consumption ends.
``savings = potential − regret``
    What the control was worth against self-consumption, ends equalised.
``capture = savings ÷ potential``
    The share of the potential that was realized.  ``None`` when the
    potential is too small to divide by.

The site's load is everything that is not the battery — house, EV and losses
— taken from the meters as ``grid_import − grid_export − battery_charged +
battery_discharged``.  The EV is therefore a fixed load: what its scheduling
saved is not measured.

Pure functions — no I/O, no Home Assistant imports.  The oracle needs scipy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.planner.hindsight_oracle import (
    ORACLE_TIME_LIMIT_S,
    HindsightBattery,
    HindsightRun,
    HindsightSlot,
    grid_cost,
    simulate_self_consumption,
    solve_hindsight_oracle,
    stored_trajectory,
)
from custom_components.hsem.utils.misc import clamp_efficiency
from custom_components.hsem.utils.units import fuse_max_energy_per_slot_kwh

#: Smallest potential, in the currency of the prices, that capture is divided
#: by.  Below it the share is noise and is reported as unknown.
MIN_POTENTIAL = 0.05

#: Energy below which a slot has no PV surplus, and a surplus counts as stored
#: in full.
_ENERGY_EPS = 1e-9

#: Share of a configured limit the realized window may exceed before the score
#: says so.  Measured battery flows run through the *configured* efficiency
#: drift by a few percent of the capacity on an ordinary day; limits that
#: describe another battery are off by far more.
LIMIT_NOTE_SHARE = 0.10


@dataclass(frozen=True)
class SiteLimits:
    """The hard limits of one installation, as the planner was configured.

    Attributes:
        rated_kwh: Rated battery capacity.
        min_soc_pct: Hardware end-of-discharge SoC.
        max_soc_pct: Charging cut-off SoC.
        max_charge_kw: Battery charge power limit.
        max_discharge_kw: Battery discharge power limit.
        charge_efficiency: Charge efficiency as a fraction.
        discharge_efficiency: Discharge efficiency as a fraction.
        export_fee_per_kwh: Fee subtracted from the export price.
        max_grid_import_kw: Main-fuse import limit, or ``None``.
        max_grid_export_kw: Grid export limit, or ``None``.
        battery_export_enabled: Whether the user lets the battery export to
            the grid at all.
        battery_export_min_price: Raw export price below which the user
            forbids battery export.
    """

    rated_kwh: float
    min_soc_pct: float
    max_soc_pct: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float
    discharge_efficiency: float
    export_fee_per_kwh: float = 0.0
    max_grid_import_kw: float | None = None
    max_grid_export_kw: float | None = None
    battery_export_enabled: bool = True
    battery_export_min_price: float = 0.0

    @classmethod
    def from_planner_input(cls, inp: PlannerInput) -> SiteLimits:
        """Read the limits from a planner input.

        Only what the hardware and the user's own settings forbid is taken:
        capacity, power, efficiencies, fuse, export cap, the export fee and
        whether (and from which price) the battery may export.  Floors,
        reserves, targets and the depreciation-based export threshold are
        HSEM's policy and stay out of the oracle.

        Args:
            inp: A planner input of the installation being scored.

        Returns:
            The installation's hard limits.
        """
        charge_kw = inp.battery_max_charge_power_w / 1000.0
        discharge_w = inp.battery_max_discharge_power_w
        fuse_kw = (
            fuse_max_energy_per_slot_kwh(inp.main_fuse_amps, inp.main_fuse_phases, 1.0)
            if inp.main_fuse_amps
            else 0.0
        )
        return cls(
            rated_kwh=inp.battery_rated_capacity_kwh,
            min_soc_pct=inp.battery_end_of_discharge_soc_pct,
            max_soc_pct=inp.battery_max_soc_pct,
            max_charge_kw=charge_kw,
            max_discharge_kw=(
                discharge_w / 1000.0 if discharge_w is not None else charge_kw
            ),
            charge_efficiency=clamp_efficiency(inp.battery_charge_efficiency_pct),
            discharge_efficiency=clamp_efficiency(inp.battery_discharge_efficiency_pct),
            export_fee_per_kwh=inp.export_fee_per_kwh,
            max_grid_import_kw=fuse_kw if fuse_kw > 0.0 else None,
            max_grid_export_kw=inp.max_grid_export_power_kw,
            battery_export_enabled=inp.excess_export_enabled,
            battery_export_min_price=max(
                inp.export_min_price, inp.battery_export_min_price
            ),
        )

    def battery(self) -> HindsightBattery:
        """Return the battery's hard limits for the hindsight functions."""
        return HindsightBattery(
            min_stored_kwh=self.rated_kwh * self.min_soc_pct / 100.0,
            max_stored_kwh=self.rated_kwh * self.max_soc_pct / 100.0,
            max_charge_kw=self.max_charge_kw,
            max_discharge_kw=self.max_discharge_kw,
            charge_efficiency=self.charge_efficiency,
            discharge_efficiency=self.discharge_efficiency,
        )

    def battery_may_export(self, export_price: float) -> bool:
        """Return whether the user's settings let the battery export at a price.

        Args:
            export_price: The slot's raw export price, before the export fee.

        Returns:
            ``False`` when battery export is switched off or the price is
            below the user's export floor.
        """
        return (
            self.battery_export_enabled
            and export_price >= self.battery_export_min_price
        )


def battery_flows_from_soc(
    soc_from_pct: float, soc_to_pct: float, site: SiteLimits
) -> tuple[float, float]:
    """Return the AC charge and discharge behind a change in battery SoC.

    The inverse of the hindsight battery model: storing ``Δ`` kWh takes
    ``Δ ÷ charge efficiency`` on the AC side, and removing it delivers
    ``Δ × discharge efficiency``.  Flows derived this way add up to the
    measured SoC exactly, so a window ends with the stored energy the battery
    really had.  A slot in which the battery both charged and discharged
    shows only the net of the two.

    Args:
        soc_from_pct: SoC at the start.
        soc_to_pct: SoC at the end.
        site: Supplies the rated capacity and the efficiencies.

    Returns:
        ``(charged_kwh, discharged_kwh)``; at most one of them is non-zero.
    """
    stored = site.rated_kwh * (soc_to_pct - soc_from_pct) / 100.0
    if stored > 0.0:
        return stored / site.charge_efficiency, 0.0
    return 0.0, -stored * site.discharge_efficiency


@dataclass(frozen=True)
class RealizedSlot:
    """One slot of a window, as the meters recorded it.

    Attributes:
        hours: Slot duration in hours.
        grid_import_kwh: Measured grid import.
        grid_export_kwh: Measured grid export.
        charged_kwh: Measured battery charge.
        discharged_kwh: Measured battery discharge.
        import_price: Price per kWh imported.
        export_price: Raw price per kWh exported, before the export fee.
        pv_kwh: Measured PV production, or ``None`` when it was not observed.
            Only bounds how much PV the oracle may curtail.
    """

    hours: float
    grid_import_kwh: float
    grid_export_kwh: float
    charged_kwh: float
    discharged_kwh: float
    import_price: float
    export_price: float
    pv_kwh: float | None = None

    @property
    def net_load_kwh(self) -> float:
        """Return everything that is not the battery: load minus PV."""
        return (
            self.grid_import_kwh
            - self.grid_export_kwh
            - self.charged_kwh
            + self.discharged_kwh
        )


@dataclass(frozen=True)
class RealizedWindow:
    """A run of consecutive realized slots, prepared for the hindsight functions.

    Attributes:
        slots: The window as it happened: load, prices and curtailable PV.
        configured: The battery's limits as configured.
        widened: The same limits, widened to contain the realized window.
        caps: Per-slot battery and grid limits, never below what was measured.
        start_kwh: Stored energy at the start of the window.
        grid_import_kwh: Measured grid import per slot.
        grid_export_kwh: Measured grid export per slot.
        charged_kwh: Measured battery charge per slot.
        discharged_kwh: Measured battery discharge per slot.
        stored_kwh: Stored energy after each slot, from the measured battery
            flows under the configured efficiencies.
        capacity_drift_kwh: How far that trajectory leaves the configured
            capacity, 0 when it stays inside.
        notes: Anything about the data that qualifies a score.
    """

    slots: tuple[HindsightSlot, ...]
    configured: HindsightBattery
    widened: HindsightBattery
    caps: dict[str, list[float] | None]
    start_kwh: float
    grid_import_kwh: tuple[float, ...]
    grid_export_kwh: tuple[float, ...]
    charged_kwh: tuple[float, ...]
    discharged_kwh: tuple[float, ...]
    stored_kwh: tuple[float, ...]
    capacity_drift_kwh: float = 0.0
    notes: tuple[str, ...] = ()

    @property
    def realized_cost(self) -> float:
        """Return the grid cost that was paid."""
        return grid_cost(self.slots, self.grid_import_kwh, self.grid_export_kwh)

    def oracle(
        self,
        min_end_stored_kwh: float,
        *,
        time_limit_s: float | None = None,
        pv_as_realized: bool = False,
    ) -> HindsightRun | None:
        """Return the oracle that ends the window with at least the given energy.

        Its limits contain the realized window, so it is a lower bound on the
        realized cost whenever *min_end_stored_kwh* is at most the energy the
        window really ended with.

        Args:
            min_end_stored_kwh: Least stored energy after the last slot.
            time_limit_s: Wall-clock limit for the solve; the oracle's own
                default when ``None``.
            pv_as_realized: Hold the handling of PV surplus as it happened and
                optimise only the rest: see :meth:`pv_charge_bounds`.  The
                result lies between the unrestricted oracle and the realized
                cost.
        """
        slots: Sequence[HindsightSlot] = self.slots
        max_charge = self.caps["max_charge_kwh"]
        min_charge: list[float] | None = None
        if pv_as_realized:
            # What was produced was not curtailed further, either.
            slots = [replace(slot, curtailable_kwh=0.0) for slot in self.slots]
            min_charge, max_charge = self.pv_charge_bounds()
        return solve_hindsight_oracle(
            slots,
            self.widened,
            self.start_kwh,
            min_end_stored_kwh,
            max_charge_kwh=max_charge,
            min_charge_kwh=min_charge,
            max_discharge_kwh=self.caps["max_discharge_kwh"],
            max_grid_import_kwh=self.caps["max_grid_import_kwh"],
            max_grid_export_kwh=self.caps["max_grid_export_kwh"],
            time_limit_s=ORACLE_TIME_LIMIT_S if time_limit_s is None else time_limit_s,
        )

    def pv_charge_bounds(self) -> tuple[list[float], list[float]]:
        """Return per-slot charge bounds that hold the PV handling as realized.

        A slot's PV surplus is what its PV left over after the load.  The
        realized run stored some of it and exported the rest, and that split
        is what is held:

        - no surplus: the slot is left free (any charge there is grid charge);
        - surplus partly or not stored: the charge is exactly what was stored
          from PV, so the rest of the surplus is exported as it was;
        - surplus stored in full: the charge is at least the surplus, and
          anything above it is grid charge, which stays free.

        Returns:
            ``(min_charge_kwh, max_charge_kwh)`` per slot.  The realized run
            satisfies them.
        """
        caps = self.caps["max_charge_kwh"] or [
            self.widened.max_charge_kw * slot.hours for slot in self.slots
        ]
        minimum: list[float] = []
        maximum: list[float] = []
        for slot, charged, cap in zip(self.slots, self.charged_kwh, caps, strict=True):
            surplus = max(-slot.net_load_kwh, 0.0)
            if surplus <= _ENERGY_EPS:
                minimum.append(0.0)
                maximum.append(cap)
            elif charged >= surplus - _ENERGY_EPS:
                minimum.append(surplus)
                maximum.append(cap)
            else:
                minimum.append(charged)
                maximum.append(charged)
        return minimum, maximum


def _widened(
    limit: float | None, observed: Sequence[float], hours: Sequence[float]
) -> list[float] | None:
    """Return per-slot caps that never fall below what was observed."""
    if limit is None:
        return None
    return [
        max(limit * slot_hours, value)
        for value, slot_hours in zip(observed, hours, strict=True)
    ]


def _export_caps(slots: Sequence[RealizedSlot], site: SiteLimits) -> list[float] | None:
    """Return per-slot grid export caps, never below what was observed.

    The export limit applies to every slot.  Where the user's settings keep
    the battery from exporting, the cap is additionally the slot's PV
    surplus, so the oracle cannot earn money the user has ruled out.
    """
    if site.max_grid_export_kw is None and all(
        site.battery_may_export(slot.export_price) for slot in slots
    ):
        return None
    caps: list[float] = []
    for slot in slots:
        cap = (
            float("inf")
            if site.max_grid_export_kw is None
            else site.max_grid_export_kw * slot.hours
        )
        if not site.battery_may_export(slot.export_price):
            cap = min(cap, max(-slot.net_load_kwh, 0.0))
        caps.append(max(cap, slot.grid_export_kwh))
    return caps


def realized_window(
    slots: Sequence[RealizedSlot], start_kwh: float, site: SiteLimits
) -> RealizedWindow:
    """Prepare consecutive realized slots for scoring.

    The oracle's limits are the site's, widened wherever the realized window
    itself went beyond them (a meter that read a little more than the
    configured power, a stored-energy trajectory that drifts under assumed
    efficiencies).  That keeps the realized window inside the oracle's
    feasible set, which is what makes the oracle a lower bound on the realized
    cost by construction and not only on the windows tested.  A widening of
    more than 10 % of a limit is named in :attr:`RealizedWindow.notes`.

    Args:
        slots: The window's slots in chronological order, at least one.
        start_kwh: Stored energy at the start of the window.
        site: The installation's hard limits.

    Returns:
        The prepared window.

    Raises:
        ValueError: If *slots* is empty.
    """
    if not slots:
        raise ValueError("a window needs at least one slot")
    hours = [slot.hours for slot in slots]
    grid_import = [slot.grid_import_kwh for slot in slots]
    grid_export = [slot.grid_export_kwh for slot in slots]
    charged = [slot.charged_kwh for slot in slots]
    discharged = [slot.discharged_kwh for slot in slots]

    configured = site.battery()
    realized = stored_trajectory(configured, start_kwh, charged, discharged)
    notes: list[str] = []
    low = min(configured.min_stored_kwh, start_kwh, *realized)
    high = max(configured.max_stored_kwh, start_kwh, *realized)
    drift = max(configured.min_stored_kwh - low, high - configured.max_stored_kwh)
    usable = configured.max_stored_kwh - configured.min_stored_kwh
    if drift > LIMIT_NOTE_SHARE * usable:
        notes.append(
            f"measured battery flows leave the configured capacity by "
            f"{drift:.2f} kWh under the configured efficiencies; the oracle's "
            f"capacity was widened to contain them"
        )
    over_charge = max(
        value - (1.0 + LIMIT_NOTE_SHARE) * configured.max_charge_kw * slot_hours
        for value, slot_hours in zip(charged, hours, strict=True)
    )
    over_discharge = max(
        value - (1.0 + LIMIT_NOTE_SHARE) * configured.max_discharge_kw * slot_hours
        for value, slot_hours in zip(discharged, hours, strict=True)
    )
    if max(over_charge, over_discharge) > 0.0:
        longest = max(hours)
        notes.append(
            f"measured battery power exceeds the configured limit: up to "
            f"{max(charged):.2f} kWh charged and {max(discharged):.2f} kWh "
            f"discharged in a slot, against "
            f"{configured.max_charge_kw * longest:.2f} and "
            f"{configured.max_discharge_kw * longest:.2f}; the oracle's limits "
            f"were widened"
        )
    return RealizedWindow(
        slots=tuple(
            HindsightSlot(
                hours=slot.hours,
                net_load_kwh=slot.net_load_kwh,
                import_price=slot.import_price,
                export_price=slot.export_price - site.export_fee_per_kwh,
                curtailable_kwh=max(slot.pv_kwh or 0.0, 0.0),
            )
            for slot in slots
        ),
        configured=configured,
        widened=replace(configured, min_stored_kwh=low, max_stored_kwh=high),
        caps={
            "max_charge_kwh": _widened(configured.max_charge_kw, charged, hours),
            "max_discharge_kwh": _widened(
                configured.max_discharge_kw, discharged, hours
            ),
            "max_grid_import_kwh": _widened(
                site.max_grid_import_kw, grid_import, hours
            ),
            "max_grid_export_kwh": _export_caps(slots, site),
        },
        start_kwh=start_kwh,
        grid_import_kwh=tuple(grid_import),
        grid_export_kwh=tuple(grid_export),
        charged_kwh=tuple(charged),
        discharged_kwh=tuple(discharged),
        stored_kwh=tuple(realized),
        capacity_drift_kwh=max(drift, 0.0),
        notes=tuple(notes),
    )


def capture_share(savings: float, potential: float) -> float | None:
    """Return the share of a potential that was realized.

    ``savings ÷ potential``, or ``None`` when the potential is below
    :data:`MIN_POTENTIAL`: a window in which no control could have saved
    anything has no share to report, and clamping it to 0 or 100 % would
    invent one.  A negative share is returned as it is.

    Args:
        savings: What the control was worth against self-consumption.
        potential: What the best control could have saved over none.
    """
    if potential < MIN_POTENTIAL:
        return None
    return savings / potential


def utilisation_share(loss: float | None, potential: float) -> float | None:
    """Return the share of a potential that one source of loss left intact.

    ``1 − loss ÷ potential``.  The losses of all sources add up to the regret,
    so their shortfalls from 100 % add up to the shortfall of the capture.
    ``None`` without a loss figure and when the potential is below
    :data:`MIN_POTENTIAL`; never clamped.

    Args:
        loss: Regret attributed to one source, or ``None`` if unknown.
        potential: What the best control could have saved over none.
    """
    if loss is None or potential < MIN_POTENTIAL:
        return None
    return 1.0 - loss / potential


@dataclass(frozen=True)
class WindowScore:
    """The score of one realized window.

    Attributes:
        realized_cost: Grid cost that was paid.
        oracle_cost: Grid cost of the perfect-foresight oracle that ends the
            window with at least the stored energy the battery really ended
            with.
        baseline_cost: Grid cost of plain self-consumption from the stored
            energy the window really started with.
        potential: ``baseline_cost`` minus the cost of the oracle that ends
            where self-consumption ends.
        realized_end_kwh: Stored energy the realized window ends with, from
            the measured battery flows.
        baseline_end_kwh: Stored energy ``baseline_cost``'s run ends with.
        pv_as_realized_cost: Grid cost of the oracle that keeps the realized
            handling of PV surplus and optimises the rest, ending like
            ``oracle_cost``'s run.  It splits the regret in two (issue #1270);
            ``None`` when the split was not asked for or could not be solved.
    """

    realized_cost: float
    oracle_cost: float
    baseline_cost: float
    potential: float
    realized_end_kwh: float
    baseline_end_kwh: float
    pv_as_realized_cost: float | None = None

    @property
    def regret(self) -> float:
        """Return ``realized − oracle``: what was left on the table."""
        return self.realized_cost - self.oracle_cost

    @property
    def savings(self) -> float:
        """Return what the control was worth against self-consumption.

        ``potential − regret``: both runs start where the window started, and
        the difference in where they end is priced by the oracle.
        """
        return self.potential - self.regret

    @property
    def capture(self) -> float | None:
        """Return the share of the potential that was realized, if it has one."""
        return capture_share(self.savings, self.potential)

    @property
    def battery_loss(self) -> float | None:
        """Return the regret that better battery dispatch would have removed.

        ``realized − pv_as_realized``: what charging from the grid and
        discharging at better times would have saved, with the PV surplus
        stored and exported exactly as it was.
        """
        if self.pv_as_realized_cost is None:
            return None
        return self.realized_cost - self.pv_as_realized_cost

    @property
    def solar_loss(self) -> float | None:
        """Return the regret that better use of the PV surplus would have removed.

        ``pv_as_realized − oracle``: what storing or exporting the surplus at
        better times would have saved on top.  Together with
        :attr:`battery_loss` it is the whole regret.
        """
        if self.pv_as_realized_cost is None:
            return None
        return self.pv_as_realized_cost - self.oracle_cost

    @property
    def battery_utilisation(self) -> float | None:
        """Return the share of the potential not lost through battery dispatch."""
        return utilisation_share(self.battery_loss, self.potential)

    @property
    def solar_utilisation(self) -> float | None:
        """Return the share of the potential not lost through PV surplus handling."""
        return utilisation_share(self.solar_loss, self.potential)


def score_window(
    window: RealizedWindow,
    *,
    baseline: HindsightRun | None = None,
    time_limit_s: float | None = None,
    breakdown: bool = False,
) -> WindowScore | None:
    """Score a prepared window.

    Args:
        window: The window, from :func:`realized_window`.
        baseline: The window's self-consumption run when the caller already
            simulated it; simulated here otherwise.
        time_limit_s: Wall-clock limit for each oracle solve; the oracle's
            own default when ``None``.
        breakdown: Also split the regret into a battery and a solar part,
            which takes a third solve.  If that solve fails the score is
            still returned, without the split.

    Returns:
        The score, or ``None`` when an oracle could not be solved to proven
        optimality (scipy missing, no feasible run, or the time limit).
    """
    if baseline is None:
        baseline = simulate_self_consumption(
            window.slots, window.configured, window.start_kwh
        )
    realized_end = window.stored_kwh[-1]
    baseline_end = baseline.stored_kwh[-1]
    # Potential: self-consumption and the oracle from the same start to the
    # same end, so the pair is comparable.
    oracle = window.oracle(realized_end, time_limit_s=time_limit_s)
    matched = window.oracle(baseline_end, time_limit_s=time_limit_s)
    if oracle is None or matched is None:
        return None
    pv_as_realized = (
        window.oracle(realized_end, time_limit_s=time_limit_s, pv_as_realized=True)
        if breakdown
        else None
    )
    return WindowScore(
        realized_cost=window.realized_cost,
        oracle_cost=oracle.cost,
        baseline_cost=baseline.cost,
        potential=baseline.cost - matched.cost,
        realized_end_kwh=realized_end,
        baseline_end_kwh=baseline_end,
        pv_as_realized_cost=None if pv_as_realized is None else pv_as_realized.cost,
    )
