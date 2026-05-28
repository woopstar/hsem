"""Async-safe logger for HSEM sensor pipeline modules.

Single responsibility: provide :func:`async_logger`, the single logging
entry-point used throughout the HSEM pipeline.

Splitting this out of ``utils/misc.py`` removes the ``_hsem_verbose_logging``
coupling from the old sensor and allows the new pipeline modules (which carry
their verbose flag inside ``self._cfg.verbose_logging``) to share the same
logger without circular imports.

Logging strategy
----------------
HSEM writes a **dedicated log file** (``hsem.log`` in the Home Assistant
config directory) via a ``RotatingFileHandler``.  This keeps the planner's
high-volume debug output out of ``home-assistant.log`` while preserving full
diagnostic detail for troubleshooting.

The HSEM logger does **not** propagate to Home Assistant's root logger
(``propagate`` is set to ``False``).  All HSEM messages — from both the
async sensor layer (``async_logger``) and the synchronous planner engine
(``log_planner``) — are captured solely by the file handler.

Users who want HSEM messages in the main HA log can enable them via the
standard ``logger:`` YAML block in ``configuration.yaml``::

    logger:
      logs:
        custom_components.hsem: debug  # also appears in home-assistant.log

Why a dedicated file instead of reusing ``home-assistant.log``:

* Home Assistant's ``home-assistant.log`` is typically kept small (weeks) and
  uses a single-file-at-a-time rotation scheme.  HSEM's verbose planner debug
  can produce thousands of lines per cycle, rapidly filling and rotating the
  main log, which discards *all* HA log history.
* A dedicated ``hsem.log`` file with its own 10 MB × 5 rotation gives ample
  room for full planner debug traces without impacting the main log.

Design note — blocking I/O:

``RotatingFileHandler`` performs synchronous ``open()`` / ``write()`` /
``rotate()`` calls.  When invoked from inside the event loop (which all HSEM
async code is), Home Assistant raises a ``Detected blocking call to open``
warning.  To avoid this we delegate writes to a global ``ThreadPoolExecutor``
in both :func:`async_logger` (which is always async) and :func:`log_planner`
(which is synchronous but offloads file I/O when a running event loop is
detected, falling back to a direct write only when no loop is present).
The init/teardown methods (:func:`init_hsem_logger` / :func:`close_hsem_logger`)
are exposed as coroutines that offload file-handler setup to the executor so they can be
safely called from ``async_setup_entry`` / ``async_unload_entry``.

Verbose flag resolution order (first match wins):

1. ``self._cfg.verbose_logging``  — new pipeline sensors (``HSEMWorkingModeSensor``
   after the #282 refactor)
2. ``self._hsem_verbose_logging`` — legacy attribute kept for any remaining
   callers that have not yet migrated
3. ``True``                       — safe default so no log messages are silently
   swallowed during start-up before config is loaded
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Relative config-directory name for the HSEM log file.
_HSEM_LOG_FILENAME = "hsem.log"
# Maximum size per log file before rotation (10 MB).
_HSEM_LOG_MAX_BYTES = 10 * 1024 * 1024
# Number of rotated log files to keep.
_HSEM_LOG_BACKUP_COUNT = 2

# ---------------------------------------------------------------------------
# Shared logger
# ---------------------------------------------------------------------------

# Use the same canonical name as ``custom_components/hsem/__init__.py``.
# All HSEM modules log to this logger.
HSEM_LOGGER = logging.getLogger("custom_components.hsem")

# Accept all levels so the file handler can capture anything the planner emits.
HSEM_LOGGER.setLevel(logging.DEBUG)

# Stop propagation to Home Assistant's root logger — HSEM uses its own file.
# Users who also want HSEM messages in home-assistant.log can enable them
# via the standard ``logger:`` YAML block in configuration.yaml.
HSEM_LOGGER.propagate = False

# Global thread-pool for async file-handler I/O.
_HSEM_EXECUTOR: ThreadPoolExecutor | None = None


def _get_executor() -> ThreadPoolExecutor:
    """Return the shared HSEM executor, creating it on first call."""
    global _HSEM_EXECUTOR  # noqa: PLW0603
    if _HSEM_EXECUTOR is None:
        _HSEM_EXECUTOR = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="hsem_log"
        )
    return _HSEM_EXECUTOR


def _build_hsem_file_handler(
    hass_config_path: str,
) -> logging.handlers.RotatingFileHandler:
    """Create a ``RotatingFileHandler`` for the HSEM log file.

    Args:
        hass_config_path: Absolute path to the Home Assistant config directory.

    Returns:
        A ``RotatingFileHandler`` configured for the ``hsem.log`` file.
    """
    log_path = os.path.join(hass_config_path, _HSEM_LOG_FILENAME)
    handler = logging.handlers.RotatingFileHandler(
        filename=log_path,
        maxBytes=_HSEM_LOG_MAX_BYTES,
        backupCount=_HSEM_LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    return handler


def init_hsem_logger_sync(hass_config_path: str) -> None:
    """Set up the HSEM file handler (synchronous — call from executor or init).

    Args:
        hass_config_path: Absolute path to the Home Assistant config directory.
    """
    # Remove any existing handlers to avoid duplicates on re-init.
    HSEM_LOGGER.handlers.clear()
    handler = _build_hsem_file_handler(hass_config_path)
    HSEM_LOGGER.addHandler(handler)


def close_hsem_logger_sync() -> None:
    """Close and remove all HSEM logger handlers (synchronous).

    Must be called from the executor during teardown.
    """
    for handler in list(HSEM_LOGGER.handlers):
        handler.close()
        HSEM_LOGGER.removeHandler(handler)


async def async_init_hsem_logger(hass: object) -> None:
    """Set up the HSEM file handler (async — offloads I/O to executor).

    Call once during ``async_setup_entry``.

    Args:
        hass: Home Assistant instance (must have a ``config`` path attribute).
    """
    config_path = _resolve_hass_config(hass)
    executor = _get_executor()
    await asyncio.get_running_loop().run_in_executor(
        executor, init_hsem_logger_sync, config_path
    )


async def async_close_hsem_logger() -> None:
    """Close all HSEM logger handlers (async — offloads I/O to executor).

    Call once during ``async_unload_entry``.
    """
    executor = _get_executor()
    await asyncio.get_running_loop().run_in_executor(executor, close_hsem_logger_sync)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def async_logger(self, msg: str, level: str = "debug") -> None:
    """Emit *msg* through the HSEM file logger if verbose logging is on.

    The write is delegated to a ``ThreadPoolExecutor`` so that the
    ``RotatingFileHandler``'s synchronous ``open()`` / ``write()`` calls
    do not block the event loop.

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
# that gap by forwarding messages straight to the HSEM file logger with a
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

    This helper may be called from **synchronous** planner code while the
    event loop is running (e.g. from ``run_planner`` invoked by the
    coordinator).  To avoid the "Detected blocking call to open" warning,
    the actual file I/O is offloaded to the shared HSEM thread pool
    executor.  If no event loop is running (tests, early init) the write
    falls back to the current thread.

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

    # Offload blocking file I/O to the thread pool executor so that the
    # RotatingFileHandler's synchronous open()/write() does not block the
    # event loop.  This avoids the "Detected blocking call to open" warning.
    try:
        loop = asyncio.get_running_loop()
        executor = _get_executor()
        # Fire-and-forget: run_in_executor returns a Future, not a coroutine,
        # so we use ensure_future instead of create_task.
        asyncio.ensure_future(  # noqa: RUF006
            loop.run_in_executor(executor, log_fn, msg, *args)
        )
    except RuntimeError:
        # No running event loop (tests, early init) — fall back to
        # a direct synchronous call on the current thread.
        if args:
            log_fn(msg, *args)
        else:
            log_fn(msg)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_hass_config(hass: object) -> str:
    """Extract the HA config directory path from a Home Assistant instance.

    Args:
        hass: Home Assistant instance.

    Returns:
        Absolute path to the config directory as a string.
    """
    # Use getattr so this module stays importable without Home Assistant
    # (for unit tests, linting, etc.).
    # hass.config is a Config object, not a string — use its path() or
    # config_dir attribute to get the directory path.
    try:
        if hasattr(hass, "config"):
            config = getattr(hass, "config")
            # Config objects expose config_dir; string paths are fallback.
            if hasattr(config, "config_dir") and config.config_dir:
                return config.config_dir
            # On some HA versions config might still be a string path.
            if isinstance(config, str):
                return config
        return os.getcwd()  # fallback for tests / lint context
    except Exception:
        return os.getcwd()
