"""HSEM dedicated file logger.

Single responsibility: provide the ``HSEM_LOGGER`` logger instance and the
``log_planner`` helper for synchronous planner code.  All HSEM components
log to ``HSEM_LOGGER``, which writes to a dedicated ``hsem.log`` file.

Verbosity is controlled by the logger's own level — call
:func:`set_hsem_verbose` once per coordinator cycle to sync with the
user's ``verbose_logging`` config setting.  No per-call gating is needed:
``HSEM_LOGGER.debug()`` calls are silently filtered when the level is
``WARNING`` or above.

Logging strategy
----------------
HSEM writes a **dedicated log file** (``hsem.log`` in the Home Assistant
config directory) via a ``RotatingFileHandler``.  This keeps the planner's
high-volume debug output out of ``home-assistant.log`` while preserving full
diagnostic detail for troubleshooting.

The HSEM logger does **not** propagate to Home Assistant's root logger
(``propagate`` is set to ``False``).  All HSEM messages — from both the
async sensor layer (``_LOGGER.debug()``) and the synchronous planner engine
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
warning.  To avoid this, :func:`log_planner` (called from synchronous planner
code) offloads file I/O to a global ``ThreadPoolExecutor`` when a running
event loop is detected, falling back to a direct write only when no loop is
present.  Async code simply calls ``_LOGGER.debug()`` directly — Python's
logging module handles the rest.

The init/teardown methods (:func:`init_hsem_logger` / :func:`close_hsem_logger`)
are exposed as coroutines that offload file-handler setup to the executor so they can be
safely called from ``async_setup_entry`` / ``async_unload_entry``.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
from concurrent.futures import ThreadPoolExecutor
from typing import cast

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

# Start at WARNING — verbose debug logging is enabled later by the
# coordinator via set_hsem_verbose() once the user's config is loaded.
HSEM_LOGGER.setLevel(logging.WARNING)

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
    for handler in list(HSEM_LOGGER.handlers):  # NOSONAR -- mutation-safe iteration
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


def set_hsem_verbose(enabled: bool) -> None:
    """Enable or disable verbose debug logging for all HSEM components.

    When enabled, sets the HSEM logger level to ``DEBUG`` so that all
    ``HSEM_LOGGER.debug()`` calls (coordinator cycle messages, planner
    slot-level decisions, etc.) are written to ``hsem.log``.

    When disabled, sets the level to ``WARNING`` so only warnings and
    errors are written.

    This replaces the previous dual-gating (``async_logger``'s per-call
    verbose check and ``set_planner_verbose``'s module global).  The
    logger's own level is now the single source of truth.

    Args:
        enabled: ``True`` to enable debug output; ``False`` to suppress.
    """
    HSEM_LOGGER.setLevel(logging.DEBUG if enabled else logging.WARNING)


# ---------------------------------------------------------------------------
# Planner synchronous logging helper
# ---------------------------------------------------------------------------
# The planner engine is intentionally pure Python (no HA imports).
# :func:`log_planner` offloads blocking file I/O to a thread-pool when
# called from synchronous code while the event loop is running.


def log_planner(level: str, msg: str, *args: object) -> None:
    """Write a structured log message to the HSEM log file.

    Uses ``%``-style formatting so that string interpolation is deferred
    until the handler decides to emit the record — consistent with Home
    Assistant logging conventions.

    This helper may be called from **synchronous** planner code while the
    event loop is running (e.g. from ``run_planner`` invoked by the
    coordinator).  To avoid the "Detected blocking call to open" warning,
    the actual file I/O is offloaded to the shared HSEM thread pool
    executor.  If no event loop is running (tests, early init) the write
    falls back to the current thread.

    The HSEM logger's own level gates verbosity — no separate flag check
    is needed.  Call :func:`set_hsem_verbose` once at the start of each
    coordinator cycle to sync the user's ``verbose_logging`` setting.

    Args:
        level: Log level string — one of ``"debug"``, ``"info"``,
               ``"warning"``, ``"error"``.  Unknown values fall back to
               ``"debug"``.
        msg: Log message template.  Use ``%s``, ``%d``, ``%.4f`` etc. for
             positional substitutions.
        *args: Positional arguments for the ``%``-style format template.
    """
    log_fn = getattr(HSEM_LOGGER, level.lower(), HSEM_LOGGER.debug)

    # Offload blocking file I/O to the thread pool executor so that the
    # RotatingFileHandler's synchronous open()/write() does not block the
    # event loop.  This avoids the "Detected blocking call to open" warning.
    try:
        loop = asyncio.get_running_loop()
        executor = _get_executor()
        # Fire-and-forget: run_in_executor returns a Future, not a coroutine,
        # so we use ensure_future instead of create_task.
        # Store the task to prevent premature garbage collection (S7502).
        _task = asyncio.ensure_future(  # noqa: RUF006
            loop.run_in_executor(executor, log_fn, msg, *args)
        )
    except RuntimeError:
        # No running event loop (tests, early init) — fall back to
        # a direct synchronous call on the current thread.
        if args:
            log_fn(msg, *args)
        else:
            log_fn(msg)


def async_log(level: str, msg: str, *args: object) -> None:
    """Write a log message without blocking the event loop.

    Identical to :func:`log_planner` but intended for use from **async**
    coordinator code.  The file I/O is offloaded to the shared HSEM
    thread pool executor so the ``RotatingFileHandler``'s synchronous
    ``open()``/``write()`` does not trigger Home Assistant's
    ``Detected blocking call to open`` warning.

    Args:
        level: Log level string — one of ``"debug"``, ``"info"``,
               ``"warning"``, ``"error"``.
        msg: Log message (pre-formatted or with ``%``-style placeholders).
        *args: Positional arguments for ``%``-style formatting.
    """
    log_planner(level, msg, *args)


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
                return cast(str, config.config_dir)
            # On some HA versions config might still be a string path.
            if isinstance(config, str):
                return config
        return os.getcwd()  # fallback for tests / lint context
    except Exception:
        return os.getcwd()
