"""Dataclass for an OCPP charger session state.

Each connected charger is tracked as a :class:`ChargerSession` instance,
holding the charger identity (from BootNotification), live power/energy
readings (from MeterValues), and transaction state.
"""

from __future__ import annotations

from dataclasses import dataclass
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
