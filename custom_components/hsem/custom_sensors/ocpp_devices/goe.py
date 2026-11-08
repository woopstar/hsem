"""go-e Charger device handler (issue #1383).

go-e exposes a vendor configuration key, ``ForceState``, that decides
whether the charge point delivers power at all, independent of any OCPP
transaction (issue #920). Confirmed against a go-e Charger V4 (firmware
60.6): it reads back ``Neutral`` normally and flips to ``Off`` when "stop
charge" is pressed in the go-e app. While it is ``Off`` the charger accepts
``RemoteStartTransaction``, ``RemoteStopTransaction`` and
``SetChargingProfile`` and ignores all of them, sitting in ``SuspendedEVSE``.
The key is writable, so HSEM can take back control.

No standard OCPP 1.6 message clears it: ``ChangeAvailability`` controls
Operative/Inoperative (connector ``Unavailable``), not ``SuspendedEVSE``.
"""

from __future__ import annotations

import logging

from custom_components.hsem.custom_sensors.ocpp_devices.base import (
    ChargerCommands,
    ChargerHandler,
)
from custom_components.hsem.models.ocpp_session import ChargerSession

_LOGGER = logging.getLogger(__name__)

#: go-e's key governing whether the charge point will charge at all.
FORCE_STATE_KEY = "ForceState"

#: ``ForceState`` value meaning "no local override, follow normal rules",
#: which is what lets OCPP RemoteStart/RemoteStop actually govern.
FORCE_STATE_NEUTRAL = "Neutral"

#: ``ForceState`` value meaning "do not charge", which blocks everything.
FORCE_STATE_OFF = "Off"


def _normalized(text: str) -> str:
    """Return *text* lower-cased with dashes, underscores and spaces removed."""
    return "".join(ch for ch in text.lower() if ch not in "-_ ")


class GoEHandler(ChargerHandler):
    """go-e Charger: clears the local ``ForceState`` block before a start."""

    name = "go-e"

    @classmethod
    def matches(cls, session: ChargerSession) -> bool:
        """Match a go-e by its ``BootNotification`` vendor, or by its key.

        The ``ForceState`` fallback keeps the behaviour from before device
        handlers existed: any charger reporting the key is treated as a
        go-e, so the clear never depends on the exact vendor string.
        """
        return (
            _normalized(session.vendor).startswith("goe")
            or _normalized(session.model).startswith("goecharger")
            or FORCE_STATE_KEY in session.configuration_keys
        )

    async def before_start(
        self, commands: ChargerCommands, session: ChargerSession
    ) -> None:
        """Write ``ForceState`` back to ``Neutral`` when it reads ``Off``.

        No-ops when the charger does not report the key, or reports a
        permissive value, so it is safe to call before every start.

        Args:
            commands: The OCPP senders.
            session: The charger session.
        """
        current = session.configuration_keys.get(FORCE_STATE_KEY)
        if current is None or current != FORCE_STATE_OFF:
            return
        _LOGGER.warning(
            "OCPP %s: charger is locally forced off (%s=%s) — clearing to "
            "%s so remote start can take effect",
            session.cpid,
            FORCE_STATE_KEY,
            current,
            FORCE_STATE_NEUTRAL,
        )
        if await commands.send_change_configuration(
            session.cpid, FORCE_STATE_KEY, FORCE_STATE_NEUTRAL
        ):
            # Optimistic local update; the next GetConfiguration confirms it.
            session.configuration_keys[FORCE_STATE_KEY] = FORCE_STATE_NEUTRAL


__all__ = [
    "FORCE_STATE_KEY",
    "FORCE_STATE_NEUTRAL",
    "FORCE_STATE_OFF",
    "GoEHandler",
]
