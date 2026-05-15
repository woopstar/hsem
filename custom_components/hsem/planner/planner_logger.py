"""Synchronous planner logger for the HSEM planning engine.

Single responsibility: provide a lightweight, synchronous logging helper that
emits structured debug output through the shared ``HSEM_LOGGER`` from
pure-Python planner modules that have no access to a sensor ``self`` object.

Design rationale
----------------
The planner engine is intentionally pure Python (no HA imports, no ``self``).
:func:`~custom_components.hsem.utils.logger.async_logger` requires a sensor
object for the verbose-flag resolution, so it cannot be called from planner
code.  This module bridges that gap by forwarding messages straight to
``HSEM_LOGGER``.

``HSEM_LOGGER`` no longer owns a file handler — it propagates into Home
Assistant's standard logging chain, so calls are non-blocking and respect
the level configured via the user's ``logger:`` YAML block.

Verbosity is controlled by a single module-level flag so that individual
planner callers can enable/disable detailed output without passing ``self``
down through every function call.

Usage::

    from custom_components.hsem.planner.planner_logger import (
        set_planner_verbose,
        log_planner,
    )

    # Enable from the coordinator / sensor before starting a planning run:
    set_planner_verbose(True)

    # Use inside any pure planner module:
    log_planner("debug", "slot %s recommendation=%s cost=%.4f", slot.start, rec, cost)
"""

from __future__ import annotations

from custom_components.hsem.utils.logger import HSEM_LOGGER

# ---------------------------------------------------------------------------
# Module-level verbosity gate
# ---------------------------------------------------------------------------

# Default: off.  Set to True by the coordinator / sensor before each run.
_PLANNER_VERBOSE: bool = False


def set_planner_verbose(enabled: bool) -> None:
    """Enable or disable planner debug logging.

    Should be called once per planning run from the async sensor layer
    (coordinator or ``HSEMWorkingModeSensor``) before calling
    :func:`~custom_components.hsem.planner.engine.run_planner`.

    Args:
        enabled: ``True`` to enable debug output; ``False`` to suppress.
    """
    global _PLANNER_VERBOSE  # noqa: PLW0603
    _PLANNER_VERBOSE = enabled


def is_planner_verbose() -> bool:
    """Return the current verbosity state.

    Returns:
        ``True`` when planner debug logging is active.
    """
    return _PLANNER_VERBOSE


def log_planner(level: str, msg: str, *args: object) -> None:
    """Write a structured log message to the HSEM log file.

    The message is only written when planner verbose logging is enabled
    (see :func:`set_planner_verbose`).  Uses ``%``-style formatting so that
    string interpolation is deferred until the handler decides to emit the
    record — consistent with Home Assistant logging conventions.

    Args:
        level: Log level string — one of ``"debug"``, ``"info"``,
               ``"warning"``, ``"error"``.  Unknown values fall back to
               ``"debug"``.
        msg: Log message template.  Use ``%s``, ``%d``, ``%.4f`` etc. for
             positional substitutions.
        *args: Positional arguments for the ``%``-style format template.
    """
    if not _PLANNER_VERBOSE:
        return

    log_fn = getattr(HSEM_LOGGER, level.lower(), HSEM_LOGGER.debug)
    if args:
        log_fn(msg, *args)
    else:
        log_fn(msg)
