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

    async def send_get_configuration(
        self, cpid: str
    ) -> bool:  # pragma: no cover - protocol signature only
        """Ask the charger for its full OCPP configuration."""
        ...

    async def send_trigger_message(
        self, cpid: str, requested_message: str, connector_id: int | None = 1
    ) -> bool:  # pragma: no cover - protocol signature only
        """Ask the charger to send one message now (Remote Trigger)."""
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

    async def recover_blocked_start(
        self, commands: ChargerCommands, session: ChargerSession
    ) -> None:
        """Try to unblock a charger that ignores a start (issue #1383).

        Called on a cooldown when the charger stays ``SuspendedEVSE`` after
        HSEM started it with a positive profile. Standard OCPP only: refresh
        the cached configuration with ``GetConfiguration`` (a vendor block
        set after boot shows up there) and ask for a fresh
        ``StatusNotification`` with ``TriggerMessage``.

        Args:
            commands: The OCPP senders.
            session: The charger session.
        """
        await commands.send_get_configuration(session.cpid)
        await commands.send_trigger_message(session.cpid, "StatusNotification")


__all__ = ["ChargerCommands", "ChargerHandler"]
