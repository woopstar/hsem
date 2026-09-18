"""Tests for the Home Assistant parallel-updates quality-scale rule."""

from importlib import import_module

import pytest

from homeassistant.const import Platform

from custom_components.hsem import PLATFORMS


@pytest.mark.parametrize("platform", PLATFORMS)
def test_platform_declares_parallel_updates(platform: Platform) -> None:
    """Every HSEM platform must explicitly disable parallel entity updates."""
    module_name = f"custom_components.hsem.{platform.value}"
    platform_module = import_module(module_name)

    assert hasattr(platform_module, "PARALLEL_UPDATES"), (
        f"{module_name} must export PARALLEL_UPDATES"
    )
    assert getattr(platform_module, "PARALLEL_UPDATES") == 0
