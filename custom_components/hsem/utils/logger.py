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
* Users can still get HSEM-only output by setting the log level in
  ``configuration.yaml``::

      logger:
        default: warning
        logs:
          custom_components.hsem: debug

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
