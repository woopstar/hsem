"""Dataclass for an OCPP charger session state.

Each connected charger is tracked as a :class:`ChargerSession` instance,
holding the charger identity (from BootNotification), live power/energy
readings (from MeterValues), and transaction state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ChargerSession:
    """Per-charger OCPP session state.

    Attributes:
        cpid: Charge-point identifier (from the WebSocket path).
        websocket: The live aiohttp WebSocket connection handle.
        status: Current charger status string, e.g. "Available",
            "Preparing", "Charging", "Finishing".
        vendor: Charger vendor string from BootNotification payload.
        model: Charger model string from BootNotification payload.
        firmware: Firmware version from BootNotification payload.
        serial: Serial number from BootNotification payload.
        current_power_w: Latest measured charging power in watts (from
            MeterValues).
        current_energy_wh: Latest measured energy in watt-hours (from
            MeterValues).
        transaction_id: Active OCPP transaction ID, or ``None`` when idle.
        last_heartbeat: Timestamp of the most recent Heartbeat message.
        connected_at: Timestamp when the WebSocket connection was established.
        status_changed_at: Timestamp when ``status`` last actually changed
            value (issue #894). ``None`` until the first StatusNotification
            is handled. Used to distinguish a transient status flap from a
            charger stuck reporting a non-"Charging" state.
        pending_calls: Outstanding outbound CALL message IDs awaiting a
            CALLRESULT, mapped to the action name that was sent (issue
            #906). Only tracks actions HSEM cares about confirming
            (``RemoteStartTransaction``, ``SetChargingProfile``,
            ``RemoteStopTransaction``) — popped once the matching
            CALLRESULT arrives.
        last_call_status: Most recent confirmed ``status`` value from a
            charger's CALLRESULT for each tracked action, e.g.
            ``{"SetChargingProfile": "Rejected"}`` (issue #906). Lets the
            anti-flap state machine and diagnostics distinguish "message
            written to the socket" from "charger actually accepted it".
    """

    cpid: str = ""
    websocket: Any = None
    status: str = "Available"
    vendor: str = ""
    model: str = ""
    firmware: str = ""
    serial: str = ""
    current_power_w: float = 0.0
    current_energy_wh: float = 0.0
    transaction_id: int | None = None
    last_heartbeat: datetime | None = None
    connected_at: datetime | None = None
    status_changed_at: datetime | None = None
    pending_calls: dict[str, str] = field(default_factory=dict)
    last_call_status: dict[str, str] = field(default_factory=dict)
