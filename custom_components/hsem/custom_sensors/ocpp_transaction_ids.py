"""Transaction ID rules for the OCPP server (issue #1404).

HSEM's transaction counter restarts at 1 on every HSEM restart, so a plain
counter hands out IDs the charger has already seen. :func:`next_transaction_id`
takes the clock as a floor, which keeps IDs rising across restarts without
storing anything, as lbbrhzn/ocpp does. Because IDs only rise,
:func:`is_stale_stop` can tell a late or resent ``StopTransaction`` for an
older transaction from one that ends the open transaction.
"""

from __future__ import annotations

import time
from typing import Any

# OCPP 1.6 types transactionId as integer, and charger firmware commonly
# stores it as int32. Past this (January 2038) IDs come from the counter.
TX_ID_CEILING = 2**31 - 1


def next_transaction_id(counter: int, now: float | None = None) -> int:
    """Return the next transaction ID.

    Args:
        counter: The lowest ID this run may hand out.
        now: Unix time, ``time.time()`` when omitted.

    Returns:
        ``max(counter, int(now))``, or ``counter`` once the clock passes
        :data:`TX_ID_CEILING`.
    """
    floor = int(time.time() if now is None else now)
    if floor > TX_ID_CEILING:
        return counter
    return max(counter, floor)


def is_stale_stop(open_id: int | None, stop_id: Any) -> bool:
    """Return whether a StopTransaction is for an older transaction.

    A stop without a usable ID, or with a higher one, still ends the open
    transaction: some firmware uses its own IDs, and leaving a transaction
    open on a guess is worse.

    Args:
        open_id: The open transaction's ID, ``None`` when idle.
        stop_id: ``transactionId`` from the stop payload.

    Returns:
        ``True`` when the stop must not end the open transaction.
    """
    if open_id is None:
        return False
    try:
        return int(stop_id) < open_id
    except ValueError, TypeError:
        return False
