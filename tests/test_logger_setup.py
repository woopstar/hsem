"""Tests for HSEM logger setup and config-directory resolution.

The HSEM log file lives in the Home Assistant config directory, and its
handler is installed off the event loop because opening and rotating the file
is blocking I/O. ``_resolve_hass_config`` keeps this module importable without
Home Assistant, so it accepts several shapes of ``hass``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from custom_components.hsem.utils.logger import (
    _resolve_hass_config,
    async_init_hsem_logger,
)

_MODULE = "custom_components.hsem.utils.logger"


class TestResolveHassConfig:
    """The config directory is resolved from whatever ``hass`` provides."""

    def test_config_object_exposing_config_dir(self, tmp_path: Path) -> None:
        """The normal HA shape: ``hass.config.config_dir``."""
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path)))

        assert _resolve_hass_config(hass) == str(tmp_path)

    def test_string_config_path(self, tmp_path: Path) -> None:
        """Older shapes where ``hass.config`` is itself a path string."""
        hass = SimpleNamespace(config=str(tmp_path))

        assert _resolve_hass_config(hass) == str(tmp_path)

    @pytest.mark.parametrize(
        "hass",
        [
            pytest.param(SimpleNamespace(), id="no_config_attribute"),
            pytest.param(
                SimpleNamespace(config=SimpleNamespace(config_dir="")),
                id="empty_config_dir",
            ),
            pytest.param(
                SimpleNamespace(config=SimpleNamespace()), id="config_without_dir"
            ),
            pytest.param(None, id="no_hass_at_all"),
        ],
    )
    def test_falls_back_to_the_working_directory(self, hass: object) -> None:
        """Without a usable config dir the log stays in the working directory."""
        assert _resolve_hass_config(hass) == os.getcwd()

    def test_a_raising_config_falls_back(self) -> None:
        """A ``hass`` whose attributes raise never breaks logger setup."""
        hass = MagicMock()
        type(hass).config = property(
            lambda _self: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        assert _resolve_hass_config(hass) == os.getcwd()


class TestAsyncInitHsemLogger:
    """Handler installation happens in the executor, not on the event loop."""

    @pytest.mark.asyncio
    async def test_installs_the_handler_off_the_event_loop(
        self, tmp_path: Path
    ) -> None:
        """The blocking setup runs through the HSEM executor."""
        hass = SimpleNamespace(config=SimpleNamespace(config_dir=str(tmp_path)))
        init_sync = MagicMock()

        with patch(f"{_MODULE}.init_hsem_logger_sync", init_sync):
            await async_init_hsem_logger(hass)

        init_sync.assert_called_once_with(str(tmp_path))
