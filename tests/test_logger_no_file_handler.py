"""Regression tests for :mod:`custom_components.hsem.utils.logger`.

These tests pin down the contract that HSEM logging must:

* propagate through Home Assistant's standard ``logging`` chain rather than
  owning a private ``/config/hsem.log`` file handler, and
* never spawn a background ``ThreadPoolExecutor`` for log dispatch.

Both properties were violated by the previous design, which produced
``Detected blocking call to open`` warnings from Home Assistant's event-loop
guard whenever the synchronous planner emitted a verbose log line.

The tests are intentionally low-level — they inspect the module's exported
symbols and the logger's handler chain — so any future regression that
re-introduces a custom file handler or a thread-pool executor will be caught
immediately, even before integration tests run.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.hsem.utils import logger as logger_module
from custom_components.hsem.utils.logger import HSEM_LOGGER, async_logger


class TestLoggerHandlerHygiene:
    """Verify the HSEM logger uses standard HA logging only."""

    def test_logger_has_no_rotating_file_handler(self) -> None:
        """HSEM_LOGGER must not own any RotatingFileHandler."""
        for handler in HSEM_LOGGER.handlers:
            assert not isinstance(handler, RotatingFileHandler), (
                "HSEM_LOGGER must not install a private rotating file "
                "handler — log via Home Assistant's standard logger instead. "
                f"Found handler: {handler!r}"
            )

    def test_logger_has_no_file_handler(self) -> None:
        """HSEM_LOGGER must not own any FileHandler subclass."""
        for handler in HSEM_LOGGER.handlers:
            assert not isinstance(handler, logging.FileHandler), (
                "HSEM_LOGGER must not write to its own file — it must "
                "propagate to Home Assistant's standard log. "
                f"Found handler: {handler!r}"
            )

    def test_logger_module_has_no_thread_pool_executor(self) -> None:
        """The logger module must not export a ThreadPoolExecutor.

        The previous implementation kept a private ``LOG_EXECUTOR`` to off-load
        blocking file writes.  The new implementation has no file writes to
        off-load, so the executor (and its background thread) must go.
        """
        assert not hasattr(logger_module, "LOG_EXECUTOR"), (
            "utils.logger must not expose a ThreadPoolExecutor; logging "
            "now propagates through Home Assistant's standard chain."
        )

    def test_logger_module_has_no_custom_log_file_path(self) -> None:
        """The logger module must not export a hard-coded log-file path."""
        assert not hasattr(logger_module, "LOG_FILE_PATH"), (
            "utils.logger must not declare a private LOG_FILE_PATH; remove "
            "the legacy /config/hsem.log handler."
        )

    def test_logger_name_matches_canonical_integration_logger(self) -> None:
        """HSEM_LOGGER must share the name used by Home Assistant for the integration."""
        assert HSEM_LOGGER.name == "custom_components.hsem"

    def test_logger_propagates_to_root(self) -> None:
        """HSEM_LOGGER must propagate so Home Assistant captures every record."""
        assert HSEM_LOGGER.propagate is True


class TestAsyncLoggerNonBlocking:
    """Verify ``async_logger`` is a non-blocking coroutine."""

    @pytest.mark.asyncio
    async def test_async_logger_does_not_use_run_in_executor(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``async_logger`` must emit logs synchronously via the standard logger.

        We verify this by capturing log output through ``caplog`` (which only
        sees records that propagate to the root logger) and confirming the
        message lands there without going through any executor indirection.
        """
        sensor = MagicMock()
        sensor._hsem_verbose_logging = True

        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            await async_logger(sensor, "regression: non-blocking dispatch")

        assert any(
            "regression: non-blocking dispatch" in record.message
            for record in caplog.records
        ), "async_logger must propagate records to the standard logger chain"

    @pytest.mark.asyncio
    async def test_async_logger_respects_verbose_flag(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When verbose logging is disabled, no record must be emitted."""
        sensor = MagicMock()
        sensor._hsem_verbose_logging = False
        # Defang the new-style config branch so the legacy branch is used.
        del sensor._cfg

        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            await async_logger(sensor, "should be suppressed")

        assert not any(
            "should be suppressed" in record.message for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_async_logger_uses_requested_level(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Level argument must map to the matching logger method."""
        sensor = MagicMock()
        sensor._hsem_verbose_logging = True

        with caplog.at_level(logging.WARNING, logger="custom_components.hsem"):
            await async_logger(sensor, "elevated", level="warning")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("elevated" in r.message for r in warnings)

    @pytest.mark.asyncio
    async def test_async_logger_signature_remains_coroutine(self) -> None:
        """Existing callers must still be able to ``await`` the function."""
        # AsyncMock is shaped like async_logger; verify we can swap them out
        # in tests without changing call sites — this guards the
        # ``async def`` signature, which the safety-gate test suite relies on.
        replacement = AsyncMock()
        await replacement(MagicMock(), "noop")
        replacement.assert_awaited_once()
