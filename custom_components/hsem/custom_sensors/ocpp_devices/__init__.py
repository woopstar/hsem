"""Per-charger device handlers for the OCPP server (issue #1383).

Each charger brand with behaviour beyond standard OCPP 1.6 gets one module
here, with a :class:`~.base.ChargerHandler` subclass. :func:`resolve_handler`
picks the handler for a charger from its ``BootNotification`` vendor and
model and its ``GetConfiguration`` keys. Chargers without a dedicated
handler get the generic one, which sends standard OCPP only.

Handlers are stateless, so they are resolved on every call rather than
stored on the session: a ``BootNotification`` or ``GetConfiguration``
reply arriving later is picked up without extra bookkeeping.
"""

from __future__ import annotations

from custom_components.hsem.custom_sensors.ocpp_devices.base import (
    ChargerCommands,
    ChargerHandler,
)
from custom_components.hsem.custom_sensors.ocpp_devices.goe import GoEHandler
from custom_components.hsem.models.ocpp_session import ChargerSession

#: Dedicated handlers, checked in order. The generic handler is the fallback.
HANDLERS: tuple[ChargerHandler, ...] = (GoEHandler(),)

_GENERIC = ChargerHandler()


def resolve_handler(session: ChargerSession) -> ChargerHandler:
    """Return the device handler for the charger behind *session*.

    Args:
        session: The charger session.

    Returns:
        The first matching handler in :data:`HANDLERS`, else the generic one.
    """
    for handler in HANDLERS:
        if handler.matches(session):
            return handler
    return _GENERIC


__all__ = ["HANDLERS", "ChargerCommands", "ChargerHandler", "resolve_handler"]
