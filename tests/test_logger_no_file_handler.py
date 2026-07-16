"""Regression tests for :mod:`custom_components.hsem.utils.logger`.

These tests verify that HSEM logging uses a non-blocking
``QueueHandler`` → ``QueueListener`` → ``RotatingFileHandler`` chain
for its own ``hsem.log`` file and does **not** propagate to Home Assistant's
root logger (to avoid flooding ``home-assistant.log`` with planner debug output).

Key contract:
* ``HSEM_LOGGER`` owns exactly one handler — a ``QueueHandler`` that
  feeds a background ``QueueListener`` thread.
* The ``QueueListener`` owns the ``RotatingFileHandler`` for ``hsem.log``.
* ``HSEM_LOGGER.propagate`` is ``False`` so planner detail stays out of the
  main HA log unless the user explicitly enables ``custom_components.hsem``
  in the ``logger:`` YAML block.
* ``set_hsem_verbose`` controls ``HSEM_LOGGER``'s level — the single source
  of truth for verbosity.
* ``log_planner`` offloads file I/O to a thread pool when the event loop is
  running.  Both ``log_planner`` and direct ``_LOGGER.debug()`` calls target
  ``HSEM_LOGGER`` — the ``QueueHandler`` + ``QueueListener`` chain ensures
  non-blocking behaviour.
"""

from __future__ import annotations

import io
import logging
from logging.handlers import QueueHandler
from pathlib import Path

from custom_components.hsem.utils import logger as logger_module
from custom_components.hsem.utils.logger import HSEM_LOGGER, set_hsem_verbose

# Re-import unchanged tests
_ORIGINAL = HSEM_LOGGER.handlers.copy()


def _attach_capture_handler() -> logging.StreamHandler:
    """Attach a memory handler to HSEM_LOGGER for test assertions.

    Returns the handler so callers can close/remove it after the test.
    """
    handler = logging.StreamHandler(io.StringIO())
    handler.setLevel(logging.DEBUG)
    HSEM_LOGGER.addHandler(handler)
    return handler


class TestLoggerHandlerHygiene:
    """Verify the HSEM logger uses its dedicated file handler."""

    def test_logger_owns_queue_handler(self, tmp_path: Path) -> None:
        """HSEM_LOGGER must have a QueueHandler after init."""
        config_dir = str(tmp_path)
        logger_module.init_hsem_logger_sync(config_dir)
        try:
            has_qh = any(isinstance(h, QueueHandler) for h in HSEM_LOGGER.handlers)
            assert has_qh, (
                "HSEM_LOGGER must have a QueueHandler for hsem.log "
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

    def test_logger_default_level_is_warning(self) -> None:
        """HSEM_LOGGER defaults to WARNING until set_hsem_verbose is called."""
        assert HSEM_LOGGER.level == logging.WARNING, (
            "HSEM_LOGGER must start at WARNING to avoid log spam "
            "before the config is loaded. "
            f"Got: {logging.getLevelName(HSEM_LOGGER.level)}"
        )

    def test_level_is_debug_after_set_hsem_verbose_true(self) -> None:
        """set_hsem_verbose(True) must set HSEM_LOGGER to DEBUG."""
        set_hsem_verbose(True)
        try:
            assert HSEM_LOGGER.level == logging.DEBUG
            assert HSEM_LOGGER.isEnabledFor(logging.DEBUG)
            assert HSEM_LOGGER.isEnabledFor(logging.INFO)
        finally:
            set_hsem_verbose(False)

    def test_level_is_warning_after_set_hsem_verbose_false(self) -> None:
        """set_hsem_verbose(False) must restore WARNING level."""
        set_hsem_verbose(True)
        set_hsem_verbose(False)
        assert HSEM_LOGGER.level == logging.WARNING

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
            count = sum(1 for h in HSEM_LOGGER.handlers if isinstance(h, QueueHandler))
            assert count == 1, f"Expected 1 QueueHandler, got {count}"
        finally:
            logger_module.close_hsem_logger_sync()


class TestVerboseLogging:
    """Verify set_hsem_verbose gating via logger level."""

    def test_debug_writes_when_verbose_enabled(self) -> None:
        """_LOGGER.debug() must write when set_hsem_verbose(True)."""
        set_hsem_verbose(True)
        handler = _attach_capture_handler()
        try:
            HSEM_LOGGER.debug("verbose: test message")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "verbose: test message" in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()
            set_hsem_verbose(False)

    def test_debug_suppressed_when_verbose_disabled(self) -> None:
        """_LOGGER.debug() must not write when verbose is off."""
        set_hsem_verbose(False)
        handler = _attach_capture_handler()
        try:
            HSEM_LOGGER.debug("should be suppressed")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "should be suppressed" not in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()

    def test_warning_always_writes_regardless_of_verbose(self) -> None:
        """_LOGGER.warning() must write even when verbose is off."""
        set_hsem_verbose(False)
        handler = _attach_capture_handler()
        try:
            HSEM_LOGGER.warning("must appear")
            stream = handler.stream
            stream.seek(0)
            output = stream.read()
            assert "must appear" in output
        finally:
            HSEM_LOGGER.removeHandler(handler)
            handler.close()


class TestPlannerLogger:
    """End-to-end: ``log_planner`` records reach the HSEM log file handler."""

    def test_log_planner_info_reaches_hsem_logger(
        self,
    ) -> None:
        """log_planner('info', ...) must write to HSEM_LOGGER."""
        from custom_components.hsem.utils.logger import log_planner

        set_hsem_verbose(True)
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
            set_hsem_verbose(False)

    def test_log_planner_debug_reaches_hsem_logger(
        self,
    ) -> None:
        """DEBUG records must also write to HSEM_LOGGER."""
        from custom_components.hsem.utils.logger import log_planner

        set_hsem_verbose(True)
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
            set_hsem_verbose(False)

    def test_log_planner_suppressed_when_not_verbose(
        self,
    ) -> None:
        """With verbose disabled, ``log_planner`` must emit nothing."""
        from custom_components.hsem.utils.logger import log_planner

        set_hsem_verbose(False)
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
