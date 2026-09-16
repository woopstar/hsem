"""Anti-flap state machine states (OCPP 1.6).

Kept in its own leaf module — importing nothing from the package — because the
enum is needed by both :mod:`ocpp_anti_flap` (which owns the state machine) and
:mod:`ocpp_commands` (which resets it), and those two already import each other
in one direction.  Defining it in either would create an import cycle.

Usage
-----
>>> from custom_components.hsem.custom_sensors.ocpp_flap_state import FlapState
>>> self._flap_state = FlapState.Idle
"""

from enum import StrEnum


class FlapState(StrEnum):
    """States of the anti-flap start/stop state machine.

    The machine runs ``Idle`` → ``Starting`` → ``Charging`` → ``Stopping`` →
    ``Idle``, with the start and stop windows gating each transition. See
    :class:`~custom_sensors.ocpp_anti_flap.OCPPAntiFlapMixin` for the
    transitions themselves.

    ``StrEnum`` rather than ``Enum``: the value is exposed as the
    ``anti_flap_state`` entity attribute and carried through
    :class:`~coordinator_data.CoordinatorData`, so members must keep comparing
    equal to — and serialising as — the previous raw strings. That makes this
    refactor behaviour-preserving with no migration.
    """

    Idle = "idle"
    """No charge target held; no command in flight."""

    Starting = "starting"
    """A positive target is being held through the start window."""

    Charging = "charging"
    """A start command was sent and the charger is expected to be drawing."""

    Stopping = "stopping"
    """A zero target is being held through the stop window."""
