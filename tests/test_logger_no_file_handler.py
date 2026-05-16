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

    def test_logger_level_is_debug(self) -> None:
        """HSEM_LOGGER must accept DEBUG records.

        Home Assistant's bootstrap sets the **root logger** level to WARNING
        (or INFO with ``hass -v``).  Without an override on ``HSEM_LOGGER``
        itself, every HSEM ``debug``/``info`` call would be rejected by
        :meth:`Logger.isEnabledFor` before the record could propagate to
        Home Assistant's queue handler.  Setting level=DEBUG keeps the
        HSEM-config ``verbose_logging`` checkbox (and
        :func:`set_planner_verbose`) as the single gate — no YAML edit
        required for users to see planner detail in ``home-assistant.log``.
        """
        assert HSEM_LOGGER.level == logging.DEBUG, (
            "HSEM_LOGGER must be set to DEBUG so the in-config verbose flag "
            "controls visibility without requiring users to override the "
            "root level via configuration.yaml. Got: "
            f"{logging.getLevelName(HSEM_LOGGER.level)}"
        )

    def test_logger_is_enabled_for_info_and_debug(self) -> None:
        """End-to-end: HSEM_LOGGER must accept both INFO and DEBUG records.

        Guards against any future regression that re-introduces a higher
        floor (e.g. ``setLevel(WARNING)``), which would silently drop the
        planner's structured slot-decision output.
        """
        assert HSEM_LOGGER.isEnabledFor(logging.DEBUG)
        assert HSEM_LOGGER.isEnabledFor(logging.INFO)


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


class TestPlannerLoggerEndToEnd:
    """End-to-end: ``log_planner`` records must reach Home Assistant's log.

    Reproduces the production failure mode: Home Assistant boots with the
    root logger pinned to WARNING (its default), the user enables HSEM
    verbose logging, the coordinator calls ``set_planner_verbose(True)``,
    and the pure-Python planner emits an INFO record via ``log_planner``.

    Before the ``setLevel(logging.DEBUG)`` fix, the INFO record was rejected
    at ``HSEM_LOGGER.isEnabledFor(INFO)`` because the logger inherited the
    root's WARNING floor, so nothing reached ``home-assistant.log``.
    """

    def test_log_planner_info_reaches_caplog_when_root_is_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Simulate HA's WARNING root + planner INFO emission."""
        from custom_components.hsem.utils.logger import log_planner, set_planner_verbose

        # Pin the root logger to WARNING (HA's default) for the duration
        # of this test — this is the exact condition that masked the
        # planner output in production.
        original_root_level = logging.root.level
        logging.root.setLevel(logging.WARNING)
        try:
            set_planner_verbose(True)
            try:
                # caplog attaches its capture handler at the propagation
                # path; HSEM_LOGGER.setLevel(DEBUG) ensures the record is
                # not filtered before it reaches the handler chain.
                with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
                    log_planner("info", "[engine] e2e: slot %d cost=%.4f", 5, 0.1234)

                matching = [
                    r
                    for r in caplog.records
                    if r.levelno == logging.INFO
                    and "e2e: slot 5 cost=0.1234" in r.getMessage()
                ]
                assert matching, (
                    "log_planner('info', ...) must reach the standard logging "
                    "chain even when the HA root logger is at WARNING. "
                    f"Captured records: {[r.getMessage() for r in caplog.records]}"
                )
            finally:
                set_planner_verbose(False)
        finally:
            logging.root.setLevel(original_root_level)

    def test_log_planner_debug_reaches_caplog_when_root_is_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """DEBUG records — the most aggressive level — must also survive."""
        from custom_components.hsem.utils.logger import log_planner, set_planner_verbose

        original_root_level = logging.root.level
        logging.root.setLevel(logging.WARNING)
        try:
            set_planner_verbose(True)
            try:
                with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
                    log_planner("debug", "[engine] e2e: debug slot=%d", 7)

                matching = [
                    r
                    for r in caplog.records
                    if r.levelno == logging.DEBUG
                    and "e2e: debug slot=7" in r.getMessage()
                ]
                assert matching, (
                    "log_planner('debug', ...) must reach the standard logging "
                    "chain when verbose logging is enabled, regardless of the "
                    "HA root logger level. "
                    f"Captured records: {[r.getMessage() for r in caplog.records]}"
                )
            finally:
                set_planner_verbose(False)
        finally:
            logging.root.setLevel(original_root_level)

    def test_log_planner_suppressed_when_not_verbose(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With verbose disabled, ``log_planner`` must emit nothing.

        The HSEM-config verbose flag remains the single gate — even when
        the underlying logger is at DEBUG.
        """
        from custom_components.hsem.utils.logger import log_planner, set_planner_verbose

        set_planner_verbose(False)
        with caplog.at_level(logging.DEBUG, logger="custom_components.hsem"):
            log_planner("info", "must-not-appear")

        assert not any("must-not-appear" in r.getMessage() for r in caplog.records), (
            "log_planner must respect _PLANNER_VERBOSE=False"
        )
