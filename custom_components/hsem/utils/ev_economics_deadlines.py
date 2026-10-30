"""Configured deadlines for the EV SoC economics table (issue #1344).

Each EV has its own list of ``HH:MM`` times in the options flow
(``hsem_ev_planned_load_economics_deadlines`` /
``hsem_ev_second_planned_load_economics_deadlines``).  The flow validator,
the config reader and the economics engine all need the same reading of that
list, so it lives here: one parser, one default, one maximum.

The EV's own target deadline is not part of this list; the engine always adds
it as an extra column (see
:func:`~custom_components.hsem.planner.ev_soc_economics.compute_ev_soc_economics`).
"""

from __future__ import annotations

from collections.abc import Iterable

from custom_components.hsem.utils.datetime_utils import parse_time_of_day

#: (hour, minute) deadlines used when nothing is configured: the next 08:00
#: and the next 17:00, the fixed pair the sensor used before issue #1344.
DEFAULT_ECONOMICS_DEADLINES: tuple[tuple[int, int], ...] = ((8, 0), (17, 0))

#: Most configured deadlines per EV.  Every deadline costs one planner solve
#: per target SoC above the car's SoC (up to 5), and both EVs share one
#: 60 s executor job; with the target-deadline column and the baseline solve
#: (issue #1348) that is at most 4 x 5 + 1 = 21 solves per EV.
MAX_ECONOMICS_DEADLINES = 3


def economics_deadline_entries(raw: object) -> list[str]:
    """Return the non-blank entries of a stored deadline list.

    Accepts the list the options flow stores, or a comma-separated string
    (hand-edited entries).  Anything else yields an empty list.

    Args:
        raw: The stored option value.

    Returns:
        The entries as stripped strings, blanks removed, in input order.
    """
    if isinstance(raw, str):
        items: Iterable[object] = raw.split(",")
    elif isinstance(raw, list | tuple):
        items = raw
    else:
        return []
    return [text for item in items if (text := str(item).strip())]


def parse_economics_deadlines(raw: object) -> tuple[tuple[int, int], ...]:
    """Return the valid configured deadlines, de-duplicated and sorted.

    Invalid entries are dropped and the result is capped at
    :data:`MAX_ECONOMICS_DEADLINES`; the options flow rejects both, so this
    only matters for hand-edited or pre-validation entries.  ``None`` (option
    never stored) yields :data:`DEFAULT_ECONOMICS_DEADLINES`; an explicitly
    empty list yields no configured deadlines.

    Args:
        raw: The stored option value.

    Returns:
        ``(hour, minute)`` pairs sorted by time of day.
    """
    if raw is None:
        return DEFAULT_ECONOMICS_DEADLINES
    parsed = {
        value
        for entry in economics_deadline_entries(raw)
        if (value := parse_time_of_day(entry)) is not None
    }
    return tuple(sorted(parsed))[:MAX_ECONOMICS_DEADLINES]


def format_economics_deadline(deadline: tuple[int, int]) -> str:
    """Return the ``HH:MM`` label of an ``(hour, minute)`` deadline."""
    hour, minute = deadline
    return f"{hour:02d}:{minute:02d}"
