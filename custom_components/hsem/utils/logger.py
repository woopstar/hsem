"""Async file logger for HSEM sensor pipeline modules.

Single responsibility: provide :func:`async_logger`, the single logging
entry-point used throughout the HSEM pipeline.

Splitting this out of ``utils/misc.py`` removes the ``_hsem_verbose_logging``
coupling from the old sensor and allows the new pipeline modules (which carry
their verbose flag inside ``self._cfg.verbose_logging``) to share the same
logger without circular imports.

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
import sys
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# File-based rotating logger shared across all pipeline modules
# ---------------------------------------------------------------------------

HSEM_LOGGER = logging.getLogger("hsem_logger")
LOG_FILE_PATH = "/config/hsem.log"
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_FILE_BACKUP_COUNT = 1

if "pytest" not in sys.modules:
    _file_handler = RotatingFileHandler(
        LOG_FILE_PATH,
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
    )
    _formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    _file_handler.setFormatter(_formatter)
    HSEM_LOGGER.addHandler(_file_handler)

HSEM_LOGGER.setLevel(logging.DEBUG)
HSEM_LOGGER.propagate = False

LOG_EXECUTOR = ThreadPoolExecutor(max_workers=1)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def async_logger(self, msg: str, level: str = "debug") -> None:
    """Write *msg* to the HSEM rotating log file if verbose logging is enabled.

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
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(LOG_EXECUTOR, log_method, msg)
