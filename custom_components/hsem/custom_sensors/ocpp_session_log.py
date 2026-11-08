"""Energy per OCPP charging session and the completed-session log (issue #1392).

``StartTransaction`` carries ``meterStart`` and ``StopTransaction`` carries
``meterStop``, both in Wh and mandatory in OCPP 1.6. HSEM used to ignore
both, and ``sensor.hsem_ocpp_charger_sessions`` stayed at 0 because nothing
ever filled its log. This mixin keeps a bounded, in-memory log per OCPP
server, filled on every ``StopTransaction``.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import UTC, datetime
from typing import Any

from custom_components.hsem.models.ocpp_session import ChargerSession

# Completed sessions kept per OCPP server. The log is in memory and starts
# empty after an HA restart.
MAX_SESSION_LOG = 20

# OCPP 1.6 §7.53: an omitted StopTransaction reason means "Local".
_DEFAULT_STOP_REASON = "Local"


def parse_meter_wh(raw: Any) -> float | None:
    """Parse an OCPP meter reading in Wh.

    Args:
        raw: ``meterStart``/``meterStop`` as sent by the charger.

    Returns:
        The reading, or ``None`` when missing, not a number, or negative.
    """
    try:
        value = float(raw)
    except ValueError, TypeError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _energy_kwh(start_wh: float | None, end_wh: float | None) -> float | None:
    """Return the energy between two meter readings in kWh.

    Args:
        start_wh: Reading at the start, in Wh.
        end_wh: Reading at the end, in Wh.

    Returns:
        The energy rounded to Wh precision, or ``None`` when either reading
        is unknown or the meter went backwards.
    """
    if start_wh is None or end_wh is None or end_wh < start_wh:
        return None
    return round((end_wh - start_wh) / 1000.0, 3)


def session_energy_kwh(session: ChargerSession) -> float | None:
    """Return the energy delivered so far in the open transaction.

    Args:
        session: The charger session.

    Returns:
        Energy in kWh, or ``None`` with no open transaction or no
        ``meterStart`` (for example a transaction adopted after a restart).
    """
    if session.transaction_id is None:
        return None
    return _energy_kwh(session.meter_start_wh, session.current_energy_wh)


class OCPPSessionLogMixin:
    """Record transaction start readings and completed sessions."""

    # Assigned in OCPPServer.__init__ (a bounded deque, see MAX_SESSION_LOG).
    _completed_sessions: deque[dict[str, Any]]

    @property
    def completed_sessions(self) -> list[dict[str, Any]]:
        """Return the completed sessions, oldest first."""
        return list(self._completed_sessions)

    @staticmethod
    def _record_transaction_start(session: ChargerSession, payload: dict) -> None:
        """Store the ``StartTransaction`` meter reading and start time.

        Args:
            session: The charger session.
            payload: StartTransaction payload.
        """
        session.meter_start_wh = parse_meter_wh(payload.get("meterStart"))
        session.transaction_started_at = datetime.now(UTC)

    def _record_completed_session(self, session: ChargerSession, payload: dict) -> None:
        """Append a completed session to the log and clear the start data.

        Args:
            session: The charger session.
            payload: StopTransaction payload.
        """
        stopped_at = datetime.now(UTC)
        started_at = session.transaction_started_at
        self._completed_sessions.append(
            {
                "cpid": session.cpid,
                "transaction_id": payload.get("transactionId"),
                "started_at": started_at.isoformat() if started_at else None,
                "stopped_at": stopped_at.isoformat(),
                "duration_min": (
                    round((stopped_at - started_at).total_seconds() / 60.0)
                    if started_at
                    else None
                ),
                "energy_kwh": _energy_kwh(
                    session.meter_start_wh, parse_meter_wh(payload.get("meterStop"))
                ),
                "reason": payload.get("reason") or _DEFAULT_STOP_REASON,
            }
        )
        session.meter_start_wh = None
        session.transaction_started_at = None


__all__ = [
    "MAX_SESSION_LOG",
    "OCPPSessionLogMixin",
    "parse_meter_wh",
    "session_energy_kwh",
]
