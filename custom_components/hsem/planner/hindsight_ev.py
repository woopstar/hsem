"""Hindsight scoring of EV charging timing (issue #1271).

Stage 3 of the performance dashboard.  The energy a car was given is taken as
it was; what is scored is *when* it was delivered.

A **session** is a run of consecutive slots in which one charger had a car
plugged in and HSEM was managing the charging.  Inside it the delivered
energy may be moved; it may not cross the charge deadline.  Three hindsight
runs, all with the battery dispatched optimally under the same limits and
ending with the same stored energy, differ only in where that energy sits:

``plug-and-charge``
    Every session delivered at full charger power from its first slot — the
    charger with nobody controlling it.
``oracle``
    Delivered when it really was.  This is the stage-1 oracle.
``joint oracle``
    Delivered at the cheapest times within each session.

The joint oracle can do everything the stage-1 oracle can, so ``oracle −
joint`` is never negative, and it adds to the battery and solar losses of
stage 2 without overlap: ``realized − joint = battery + solar + EV``.

Pure functions — no I/O, no Home Assistant imports.  The solves need scipy.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from custom_components.hsem.planner.hindsight_oracle import (
    ORACLE_TIME_LIMIT_S,
    HindsightRun,
    ShiftableLoad,
    solve_hindsight_oracle,
)
from custom_components.hsem.planner.hindsight_scoring import RealizedWindow

#: Energy below which a session has nothing worth moving.
_MIN_SESSION_KWH = 1e-3


@dataclass(frozen=True)
class ChargerSlot:
    """What one EV charger did in one slot.

    Attributes:
        kwh: Energy delivered to the car, or ``None`` when not observed.
        connected: Whether a car was plugged in throughout, or ``None`` when
            the charger has no connected sensor.
        managed: Whether HSEM was managing the charging throughout: EV
            planning enabled, smart charging on, "force charge now" off.
        deadline: The charge deadline in force, if any.
        max_kw: The configured charger power, or ``None`` when not configured.
    """

    kwh: float | None
    connected: bool | None
    managed: bool
    deadline: datetime | None = None
    max_kw: float | None = None

    @property
    def movable(self) -> bool:
        """Return whether this slot's energy may be delivered at another time."""
        return self.connected is True and self.managed and self.kwh is not None


@dataclass(frozen=True)
class EvSession:
    """Energy one charger delivered that could have been delivered at other times.

    Attributes:
        realized_kwh: Energy delivered per slot of the window; 0 outside the
            session.
        max_kwh: Most the charger could have delivered per slot; 0 outside
            the session.
    """

    realized_kwh: tuple[float, ...]
    max_kwh: tuple[float, ...]

    @property
    def energy_kwh(self) -> float:
        """Return the energy the session delivered."""
        return sum(self.realized_kwh)

    def plug_and_charge_kwh(self) -> tuple[float, ...]:
        """Return the session delivered at full power from its first slot."""
        remaining = self.energy_kwh
        schedule: list[float] = []
        for cap in self.max_kwh:
            drawn = min(cap, remaining)
            schedule.append(drawn)
            remaining -= drawn
        return tuple(schedule)

    def as_load(self) -> ShiftableLoad:
        """Return the session as a load the oracle places itself."""
        return ShiftableLoad(energy_kwh=self.energy_kwh, max_kwh=self.max_kwh)


def _split_at_deadlines(
    first: int,
    last: int,
    slots: Sequence[ChargerSlot | None],
    starts: Sequence[datetime],
    hours: Sequence[float],
) -> list[tuple[int, int]]:
    """Return the run ``first..last`` cut where a charge deadline falls in it.

    The slot a deadline falls in starts the later part, so energy delivered
    in slots that ended by the deadline cannot be moved past it.  Only a
    deadline that was still ahead when a slot ended binds that slot: one that
    has passed says nothing about energy delivered after it.
    """
    cuts: set[int] = set()
    for index in range(first, last + 1):
        slot = slots[index]
        deadline = None if slot is None else slot.deadline
        if deadline is None:
            continue
        if starts[index] + timedelta(hours=hours[index]) > deadline:
            continue
        for later in range(index + 1, last + 1):
            if starts[later] + timedelta(hours=hours[later]) > deadline:
                cuts.add(later)
                break
    bounds = [first, *sorted(cuts), last + 1]
    return [(a, b - 1) for a, b in zip(bounds, bounds[1:], strict=False)]


def ev_sessions(
    slots: Sequence[ChargerSlot | None],
    starts: Sequence[datetime],
    hours: Sequence[float],
) -> list[EvSession]:
    """Return the sessions of one charger over a window.

    A session is a run of consecutive slots that are all movable (plugged in,
    managed, energy observed), cut at charge deadlines.  A run of one slot, or
    one that delivered next to nothing, has nothing to move and is left out:
    its energy stays in the load.

    Args:
        slots: The charger's record per slot of the window, ``None`` where
            there is none.
        starts: Start of each slot.
        hours: Duration of each slot in hours.

    Returns:
        The sessions, in chronological order.
    """
    count = len(slots)
    sessions: list[EvSession] = []
    index = 0
    while index < count:
        slot = slots[index]
        if slot is None or not slot.movable:
            index += 1
            continue
        last = index
        while last + 1 < count and (following := slots[last + 1]) is not None:
            if not following.movable:
                break
            last += 1
        for first, final in _split_at_deadlines(index, last, slots, starts, hours):
            session = _session(first, final, slots, hours, count)
            if session is not None:
                sessions.append(session)
        index = last + 1
    return sessions


def _session(
    first: int,
    last: int,
    slots: Sequence[ChargerSlot | None],
    hours: Sequence[float],
    count: int,
) -> EvSession | None:
    """Return the session over ``first..last``, or ``None`` if nothing can move."""
    members = [slot for slot in slots[first : last + 1] if slot is not None]
    delivered = [slot.kwh or 0.0 for slot in members]
    if last == first or sum(delivered) < _MIN_SESSION_KWH:
        return None
    configured = max((slot.max_kw or 0.0 for slot in members), default=0.0)
    # Without a configured charger power, the highest power the session
    # itself reached is what the charger is known to be capable of.
    power = configured or max(
        kwh / hours[first + offset] for offset, kwh in enumerate(delivered)
    )
    realized = [0.0] * count
    caps = [0.0] * count
    for offset, kwh in enumerate(delivered):
        realized[first + offset] = kwh
        caps[first + offset] = max(power * hours[first + offset], kwh)
    return EvSession(realized_kwh=tuple(realized), max_kwh=tuple(caps))


@dataclass(frozen=True)
class EvScore:
    """The EV timing costs of one window.

    Attributes:
        shiftable_kwh: Energy delivered in sessions, which could have been
            delivered at other times.
        joint_oracle_cost: Grid cost with the battery and the EV timing both
            chosen by hindsight, or ``None`` if that could not be solved.
        plug_and_charge_cost: Grid cost with the battery chosen by hindsight
            and every session charged from its first slot at full power, or
            ``None`` likewise.
    """

    shiftable_kwh: float
    joint_oracle_cost: float | None = None
    plug_and_charge_cost: float | None = None


def _solve(
    window: RealizedWindow,
    min_end_stored_kwh: float,
    net_delta: Sequence[float],
    *,
    shiftable: Sequence[ShiftableLoad] = (),
    extra_import: Sequence[float] | None = None,
    time_limit_s: float | None,
) -> HindsightRun | None:
    """Solve the window's oracle with its net load shifted by *net_delta*."""
    import_caps = window.caps["max_grid_import_kwh"]
    if import_caps is not None and extra_import is not None:
        import_caps = [
            cap + extra for cap, extra in zip(import_caps, extra_import, strict=True)
        ]
    return solve_hindsight_oracle(
        [
            replace(slot, net_load_kwh=slot.net_load_kwh + delta)
            for slot, delta in zip(window.slots, net_delta, strict=True)
        ],
        window.widened,
        window.start_kwh,
        min_end_stored_kwh,
        max_charge_kwh=window.caps["max_charge_kwh"],
        max_discharge_kwh=window.caps["max_discharge_kwh"],
        max_grid_import_kwh=import_caps,
        max_grid_export_kwh=window.caps["max_grid_export_kwh"],
        shiftable=shiftable,
        time_limit_s=ORACLE_TIME_LIMIT_S if time_limit_s is None else time_limit_s,
    )


def score_ev(
    window: RealizedWindow,
    sessions: Sequence[EvSession],
    *,
    time_limit_s: float | None = None,
) -> EvScore:
    """Score the EV timing of a window.

    Both runs end with at least the stored energy the window really ended
    with, like the stage-1 oracle they are compared with.

    Args:
        window: The prepared window, whose net load contains the EV energy
            as it was delivered.
        sessions: The window's sessions, of every charger.
        time_limit_s: Wall-clock limit for each solve; the oracle's own
            default when ``None``.

    Returns:
        The costs.  Without sessions nothing is solved and both are ``None``.
    """
    shiftable = sum(session.energy_kwh for session in sessions)
    if not sessions:
        return EvScore(shiftable_kwh=0.0)
    count = len(window.slots)
    realized = [sum(s.realized_kwh[i] for s in sessions) for i in range(count)]
    schedules = [session.plug_and_charge_kwh() for session in sessions]
    plugged = [sum(schedule[i] for schedule in schedules) for i in range(count)]
    end = window.stored_kwh[-1]
    joint = _solve(
        window,
        end,
        [-kwh for kwh in realized],
        shiftable=[session.as_load() for session in sessions],
        time_limit_s=time_limit_s,
    )
    # Plug-and-charge is what a charger does with no regard for the fuse, so
    # the import limit is widened by what it adds to a slot: the baseline has
    # to exist for the comparison to mean anything.
    baseline = _solve(
        window,
        end,
        [now - before for now, before in zip(plugged, realized, strict=True)],
        extra_import=[
            max(now - before, 0.0)
            for now, before in zip(plugged, realized, strict=True)
        ],
        time_limit_s=time_limit_s,
    )
    return EvScore(
        shiftable_kwh=shiftable,
        joint_oracle_cost=None if joint is None else joint.cost,
        plug_and_charge_cost=None if baseline is None else baseline.cost,
    )
