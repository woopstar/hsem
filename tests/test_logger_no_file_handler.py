"""Regression tests for :mod:`custom_components.hsem.utils.logger`.

These tests verify that HSEM logging uses a dedicated ``RotatingFileHandler``
for its own ``hsem.log`` file and does **not** propagate to Home Assistant's
root logger (to avoid flooding ``home-assistant.log`` with planner debug output).

Key contract:
* ``HSEM_LOGGER`` owns exactly one handler — a ``RotatingFileHandler`` for
  ``hsem.log`` in the HA config directory.
* ``HSEM_LOGGER.propagate`` is ``False`` so planner detail stays out of the
  main HA log unless the user explicitly enables ``custom_components.hsem``
  in the ``logger:`` YAML block.
* ``async_logger`` writes directly (no per-call executor delegation), but
  init/teardown of the file handler runs in the executor to avoid HA's
  ``Detected blocking call to open`` during setup/unload.
* ``log_planner`` and ``async_logger`` both target ``HSEM_LOGGER`` — the
  single file handler captures everything.
"""

from __future__ import annotations

import io
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.hsem.utils import logger as logger_module
from custom_components.hsem.utils.logger import HSEM_LOGGER, async_logger

# Re-import unchanged tests
_ORIGINAL = HSEM_LOGGER.handlers.copy()


def _attach_capture_handler() -> logging.Handler:
    """Attach a memory handler to HSEM_LOGGER for test assertions.

    Returns the handler so callers can close/remove it after the test.
    """
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.DEBUG)
    HSEM_LOGGER.addHandler(handler)
    return handler


class TestLoggerHandlerHygiene:
    """Verify the HSEM logger uses its dedicated file handler."""

    def test_logger_owns_rotating_file_handler(self, tmp_path: Path) -> None:
        """HSEM_LOGGER must have a RotatingFileHandler after init."""
        config_dir = str(tmp_path)
        logger_module.init_hsem_logger_sync(config_dir)
        try:
            has_rfh = any(
                isinstance(h, RotatingFileHandler) for h in HSEM_LOGGER.handlers
            )
            assert has_rfh, (
                "HSEM_LOGGER must have a RotatingFileHandler for hsem.log "
                "after init_hsem_logger_sync is called."
            )
        finally:
            logger_module.close_hsem_logger_sync()

    def test_logger_name_matches_canonical_integration_logger(self) -> None:
        """HSEM_LOGGER must share the name used by Home Assistant for the integration."""
        assert HSEM_LOGGER.name == "custom_components.hsem"

    def test_logger_does_not_propagate_to_root(self) -> None:
        """HSEM_LOGGER must not propagate to avoid flooding home-assistant.log."""
        assert HSEM_LOGGER.propagate is False

    def test_logger_level_is_debug(self) -> None:
        """HSEM_LOGGER must accept DEBUG records."""
        assert HSEM_LOGGER.level == logging.DEBUG, (
            "HSEM_LOGGER must be set to DEBUG so the in-config verbose flag "
            "controls visibility. "
            f"Got: {logging.getLevelName(HSEM_LOGGER.level)}"
        )

    def test_logger_is_enabled_for_info_and_debug(self) -> None:
        """End-to-end: HSEM_LOGGER must accept both INFO and DEBUG records."""
        assert HSEM_LOGGER.isEnabledFor(logging.DEBUG)
        assert HSEM_LOGGER.isEnabledFor(logging.INFO)

    def test_logger_module_exposes_log_file_constants(self) -> None:
        """The logger module must expose the filename constant for diagnostics."""
        assert hasattr(logger_module, "_HSEM_LOG_FILENAME")
        assert logger_module._HSEM_LOG_FILENAME == "hsem.log"

    def test_logger_has_no_duplicate_handlers_after_reinit(
        self, tmp_path: Path
    ) -> None:
        """Calling init twice must not produce duplicate handlers."""
        config_dir = str(tmp_path)
        logger_module.init_hsem_logger_sync(config_dir)
        logger_module.init_hsem_logger_sync(config_dir)
        try:
            count = sum(
                1 for h in HSEM_LOGGER.handlers if isinstance(h, RotatingFileHandler)
            )
            assert count == 1, f"Expected 1 RotatingFileHandler, got {count}"
        finally:
            logger_module.close_hsem_logger_sync()


class TestAsyncLoggerNonBlocking:
    """Verify ``async_logger`` writes directly to the HSEM logger."""

    @pytest.mark.asyncio
    async def test_async_logger_writes_to_hsem_logger(
        self,
    ) -> None:
        """``async_logger`` must emit logs through HSEM_LOGGER."""
        sensor = MagicMock()
        sensor._hsem_verbose_logging = True

        handler = _attach_capture_handler()
        try:
            await async_logger(sensor, "regression: test message")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert (
                "regression: test message" in output
            ), "async_logger must write to HSEM_LOGGER (the file handler)."
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()

    @pytest.mark.asyncio
    async def test_async_logger_respects_verbose_flag(
        self,
    ) -> None:
        """When verbose logging is disabled, no record must be emitted."""
        sensor = MagicMock()
        sensor._hsem_verbose_logging = False
        # Defang the new-style config branch so the legacy branch is used.
        del sensor._cfg

        handler = _attach_capture_handler()
        try:
            await async_logger(sensor, "should be suppressed")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "should be suppressed" not in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()

    @pytest.mark.asyncio
    async def test_async_logger_uses_requested_level(
        self,
    ) -> None:
        """Level argument must map to the matching logger method."""
        sensor = MagicMock()
        sensor._hsem_verbose_logging = True

        handler = _attach_capture_handler()
        handler.setLevel(logging.WARNING)
        try:
            await async_logger(sensor, "must appear", level="warning")
            await async_logger(sensor, "must NOT appear", level="debug")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "must appear" in output
            assert "must NOT appear" not in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()


class TestPlannerLogger:
    """End-to-end: ``log_planner`` records reach the HSEM log file handler."""

    def test_log_planner_info_reaches_hsem_logger(
        self,
    ) -> None:
        """log_planner('info', ...) must write to HSEM_LOGGER."""
        from custom_components.hsem.utils.logger import log_planner, set_planner_verbose

        set_planner_verbose(True)
        handler = _attach_capture_handler()
        try:
            log_planner("info", "[engine] e2e: slot %d cost=%.4f", 5, 0.1234)
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "e2e: slot 5 cost=0.1234" in output, (
                "log_planner('info', ...) must write to HSEM_LOGGER "
                "so it reaches hsem.log. "
            )
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()
            set_planner_verbose(False)

    def test_log_planner_debug_reaches_hsem_logger(
        self,
    ) -> None:
        """DEBUG records must also write to HSEM_LOGGER."""
        from custom_components.hsem.utils.logger import log_planner, set_planner_verbose

        set_planner_verbose(True)
        handler = _attach_capture_handler()
        try:
            log_planner("debug", "[engine] e2e: debug slot=%d", 7)
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "e2e: debug slot=7" in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()
            set_planner_verbose(False)

    def test_log_planner_suppressed_when_not_verbose(
        self,
    ) -> None:
        """With verbose disabled, ``log_planner`` must emit nothing."""
        from custom_components.hsem.utils.logger import log_planner, set_planner_verbose

        set_planner_verbose(False)
        handler = _attach_capture_handler()
        try:
            log_planner("info", "must-not-appear")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "must-not-appear" not in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()
