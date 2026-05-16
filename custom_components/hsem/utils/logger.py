"""Async-safe logger for HSEM sensor pipeline modules.

Single responsibility: provide :func:`async_logger`, the single logging
entry-point used throughout the HSEM pipeline.

Splitting this out of ``utils/misc.py`` removes the ``_hsem_verbose_logging``
coupling from the old sensor and allows the new pipeline modules (which carry
their verbose flag inside ``self._cfg.verbose_logging``) to share the same
logger without circular imports.

Logging strategy
----------------
HSEM intentionally **does not** install its own file handler.  All messages
are emitted through the standard ``custom_components.hsem`` logger, which
Home Assistant routes into ``home-assistant.log`` (and any other handler the
user configures via the ``logger:`` block in ``configuration.yaml``).

Why no custom ``/config/hsem.log``:

* ``RotatingFileHandler`` performs synchronous ``open()``/``write()``/
  ``rotate()`` calls.  When invoked from inside the event loop (which all
  HSEM async code is), Home Assistant raises a ``Detected blocking call to
  open`` warning and asks the integration author to file a bug.  The
  previous design also wrapped writes in a private ``ThreadPoolExecutor``,
  but only the ``async_logger`` callers used it — synchronous callers in
  pure-Python planner modules (``planner/planner_logger.py``) still wrote
  directly to the file handler, which is exactly the blocking-I/O code path
  Home Assistant flagged.
* Home Assistant already provides log rotation, level filtering, and a
  central log viewer.  Mirroring the same lines into a second file added
  no diagnostic value while doubling the disk-I/O cost.
* Home Assistant's bootstrap sets the **root logger** level to ``WARNING``
  (or ``INFO`` with ``hass -v``).  Without an override, every HSEM
  ``debug``/``info`` call would be filtered out before reaching the
  ``home-assistant.log`` queue handler.  We therefore set
  ``HSEM_LOGGER.setLevel(logging.DEBUG)`` so that the HSEM in-config
  *verbose_logging* checkbox (and ``set_planner_verbose``) remain the
  single source of truth: when they are ``True``, planner / pipeline
  detail flows straight into ``home-assistant.log`` without any YAML
  reconfiguration.
* Users who want still finer control can layer the standard
  ``configuration.yaml`` block on top — it overrides our default level::

      logger:
        default: warning
        logs:
          custom_components.hsem: info   # quieter than our DEBUG default

Verbose flag resolution order (first match wins):

1. ``self._cfg.verbose_logging``  — new pipeline sensors (``HSEMWorkingModeSensor``
   after the #282 refactor)
2. ``self._hsem_verbose_logging`` — legacy attribute kept for any remaining
   callers that have not yet migrated
3. ``True``                       — safe default so no log messages are silently
   swallowed during start-up before config is loaded
"""

from __future__ import annotations

import logging

# ---------------------------------------------------------------------------
# Shared logger — propagates to Home Assistant's root handlers
# ---------------------------------------------------------------------------

# Use the same canonical name as ``custom_components/hsem/__init__.py`` so
# all HSEM modules share a single configurable logger that the user can
# control via the standard Home Assistant ``logger:`` YAML block.
HSEM_LOGGER = logging.getLogger("custom_components.hsem")

# Accept records down to DEBUG so the HSEM in-config verbose_logging flag
# is the single gate that decides what is emitted.  Records still propagate
# (``propagate`` defaults to ``True``) to Home Assistant's root logger,
# whose queue handler writes them to ``home-assistant.log`` from a
# background thread — non-blocking, no private file handler required.
#
# A user-supplied ``logger:`` YAML entry for ``custom_components.hsem``
# overrides this default, so power users can still raise the floor (e.g.
# to INFO or WARNING) without code changes.
HSEM_LOGGER.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def async_logger(self, msg: str, level: str = "debug") -> None:
    """Emit *msg* through the standard HSEM logger if verbose logging is on.

    The implementation is intentionally a plain (non-blocking) call to the
    standard ``logging`` module: Python's ``logging`` handlers used by Home
    Assistant are safe to invoke from within the event loop.  The function
    remains a coroutine so callers do not need to change their ``await``
    syntax.

    Works with both the refactored sensor (``self._cfg.verbose_logging``) and
    the legacy attribute (``self._hsem_verbose_logging``) so that no callers
    need to be updated simultaneously.

    Args:
        self: Any sensor/entity instance that exposes its verbose flag via
              ``self._cfg.verbose_logging`` or ``self._hsem_verbose_logging``.
        msg: The log message to write.
        level: Log level string — one of ``"debug"``, ``"info"``,
               ``"warning"``, ``"error"``, ``"critical"``.
    """
    # Resolve verbose flag — try new config object first, then legacy attribute
    if hasattr(self, "_cfg") and hasattr(self._cfg, "verbose_logging"):
        verbose = self._cfg.verbose_logging
    elif hasattr(self, "_hsem_verbose_logging"):
        verbose = self._hsem_verbose_logging
    else:
        verbose = True  # safe default during early init

    if not verbose:
        return

    log_method = getattr(HSEM_LOGGER, level.lower(), HSEM_LOGGER.debug)
    log_method(msg)


# ---------------------------------------------------------------------------
# Planner synchronous logging helpers
# ---------------------------------------------------------------------------
# The planner engine is intentionally pure Python (no HA imports, no ``self``).
# :func:`async_logger` requires a sensor object for the verbose-flag
# resolution, so it cannot be called from planner code.  These helpers bridge
# that gap by forwarding messages straight to ``HSEM_LOGGER`` with a
# module-level verbosity gate.

_PLANNER_VERBOSE: bool = False


def set_planner_verbose(enabled: bool) -> None:
    """Enable or disable planner debug logging.

    Should be called once per planning run from the async sensor layer
    (coordinator or ``HSEMWorkingModeSensor``) before calling the planner.

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
