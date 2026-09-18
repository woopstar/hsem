"""Shared factory for coordinators built through their real ``__init__``.

Most coordinator tests use ``object.__new__`` to skip
``DataUpdateCoordinator.__init__``, which calls HA's ``frame.report_usage``
(that needs a bootstrapped runtime). Patching just that helper lets tests run
the real constructor instead, so every attribute starts at its production
default rather than a hand-picked subset.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator


def make_real_coordinator(
    options: dict[str, Any] | None = None,
    *,
    hass: MagicMock | None = None,
    config_entry: MagicMock | None = None,
) -> HSEMDataUpdateCoordinator:
    """Construct a coordinator through its real ``__init__``.

    Args:
        options: Config entry options; ignored when *config_entry* is given,
            and missing keys fall back to defaults.
        hass: Mock Home Assistant instance; a fresh ``MagicMock`` by default.
        config_entry: Ready-made config entry (e.g. from
            ``tests.test_ha_mock_integration.make_fake_config_entry``).

    Returns:
        A coordinator with production defaults for all per-cycle state.
    """
    entry = config_entry if config_entry is not None else MagicMock()
    if config_entry is None:
        entry.entry_id = "test_entry"
        entry.options = dict(options or {})
        entry.data = {}
    with patch("homeassistant.helpers.frame.report_usage"):
        return HSEMDataUpdateCoordinator(
            hass if hass is not None else MagicMock(), entry
        )
