"""Generic OCPP 1.6 charger handler (issue #1383).

A *device handler* holds the charger-specific behaviour HSEM needs on top of
standard OCPP: vendor configuration keys, quirks and recovery steps. The
generic OCPP modules (anti-flap, commands, profiles) call only the hooks
defined here, and never reference a vendor key themselves.

:class:`ChargerHandler` is also the fallback for any charger without a
dedicated handler. It sends standard OCPP only, so every hook is a no-op.

Adding a charger brand means one new module in this package, with a
:class:`ChargerHandler` subclass, plus one entry in
:data:`~custom_sensors.ocpp_devices.HANDLERS`.
"""

from __future__ import annotations

from typing import Protocol

from custom_components.hsem.models.ocpp_session import ChargerSession


class ChargerCommands(Protocol):
    """The OCPP senders a handler may use (implemented by ``OCPPServer``)."""

    async def send_change_configuration(
        self, cpid: str, key: str, value: str
    ) -> bool:  # pragma: no cover - protocol signature only
        """Write one OCPP configuration key on the charger."""
        ...


class ChargerHandler:
    """Standard-OCPP behaviour, used for any charger without its own handler."""

    #: Name shown as the ``handler`` attribute on the charger-info sensor.
    name = "generic"

    @classmethod
    def matches(cls, session: ChargerSession) -> bool:
        """Return whether this handler applies to the charger behind *session*.

        Called in :data:`~custom_sensors.ocpp_devices.HANDLERS` order. The
        generic handler matches every charger, so it is always last.

        Args:
            session: The charger session, with ``BootNotification`` vendor
                and model and the last ``GetConfiguration`` keys.
        """
        return True

    async def before_start(
        self, commands: ChargerCommands, session: ChargerSession
    ) -> None:
        """Clear any charger-local block before a remote start.

        Called before every ``RemoteStartTransaction``. A standard OCPP
        charger has nothing to clear.

        Args:
            commands: The OCPP senders.
            session: The charger session.
        """


__all__ = ["ChargerCommands", "ChargerHandler"]
