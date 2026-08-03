"""Tests for the HSEM dashboard provisioning helper."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from homeassistant.components.lovelace.const import (
    CONF_URL_PATH,
    LOVELACE_DATA,
)
from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.utils.dashboard import (
    DASHBOARD_TITLE,
    DASHBOARD_URL_PATH,
    async_ensure_hsem_dashboard,
)


@pytest.fixture
def mock_hass(tmp_path: Path) -> MagicMock:
    """Return a mocked HomeAssistant with a config directory."""
    hass = MagicMock()
    hass.config.path.return_value = str(tmp_path)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    hass.data = {}
    return hass


@pytest.fixture
def _bundled_yaml(tmp_path: Path) -> Generator[Path]:
    """Create a fake bundled dashboard YAML and patch the helper to use it."""
    dashboards_dir = tmp_path / "dashboards"
    dashboards_dir.mkdir()
    source = dashboards_dir / "dashboard_en.yaml"
    source.write_text(
        yaml.safe_dump({"views": [{"title": "HSEM", "cards": []}]}),
        encoding="utf-8",
    )

    with patch(
        "custom_components.hsem.utils.dashboard._bundled_dashboard_path",
        return_value=source,
    ):
        yield source


@pytest.mark.asyncio
async def test_create_dashboard_writes_yaml_and_registers_dashboard(
    mock_hass: MagicMock,
    tmp_path: Path,
    _bundled_yaml: Path,
) -> None:
    """Dashboard YAML is written and a Lovelace dashboard is registered."""
    collection = MagicMock()
    collection.async_items.return_value = []
    collection.async_create_item = AsyncMock(return_value={"id": "dashboard-1"})
    collection.async_delete_item = AsyncMock()

    config = MagicMock()
    config.async_save = AsyncMock()
    lovelace_data = MagicMock()
    lovelace_data.dashboards = {DASHBOARD_URL_PATH: config}
    mock_hass.data[LOVELACE_DATA] = lovelace_data

    with (
        patch(
            "custom_components.hsem.utils.dashboard._active_dashboards_collection",
            return_value=collection,
        ),
        patch(
            "custom_components.hsem.utils.dashboard.Store",
        ) as mock_store_cls,
    ):
        mock_store_cls.return_value.async_load = AsyncMock(return_value=None)
        mock_store_cls.return_value.async_save = AsyncMock()

        result = await async_ensure_hsem_dashboard(mock_hass)

    assert result["dashboard_path"] == str(tmp_path / "hsem_dashboard.yaml")
    assert result["dashboard_url"] == f"/{DASHBOARD_URL_PATH}"
    assert (tmp_path / "hsem_dashboard.yaml").exists()

    collection.async_create_item.assert_awaited_once()
    call_data = collection.async_create_item.call_args[0][0]
    assert call_data["title"] == DASHBOARD_TITLE
    assert call_data[CONF_URL_PATH] == DASHBOARD_URL_PATH
    config.async_save.assert_awaited_once()


@pytest.mark.asyncio
async def test_create_dashboard_idempotent_when_dashboard_exists(
    mock_hass: MagicMock,
    tmp_path: Path,
    _bundled_yaml: Path,
) -> None:
    """Calling the helper again is a no-op when the dashboard already exists."""
    collection = MagicMock()
    collection.async_items.return_value = [
        {"url_path": DASHBOARD_URL_PATH, "title": DASHBOARD_TITLE}
    ]
    collection.async_create_item = AsyncMock()

    with (
        patch(
            "custom_components.hsem.utils.dashboard._active_dashboards_collection",
            return_value=collection,
        ),
        patch(
            "custom_components.hsem.utils.dashboard.Store",
        ) as mock_store_cls,
    ):
        mock_store_cls.return_value.async_load = AsyncMock(return_value=None)
        mock_store_cls.return_value.async_save = AsyncMock()

        result = await async_ensure_hsem_dashboard(mock_hass)

    assert result["dashboard_url"] == f"/{DASHBOARD_URL_PATH}"
    collection.async_create_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_dashboard_respects_deleted_marker(
    mock_hass: MagicMock,
    tmp_path: Path,
    _bundled_yaml: Path,
) -> None:
    """A previously deleted dashboard is not recreated automatically."""
    collection = MagicMock()
    collection.async_items.return_value = []
    collection.async_create_item = AsyncMock()

    with (
        patch(
            "custom_components.hsem.utils.dashboard._active_dashboards_collection",
            return_value=collection,
        ),
        patch(
            "custom_components.hsem.utils.dashboard.Store",
        ) as mock_store_cls,
    ):
        mock_store_cls.return_value.async_load = AsyncMock(
            return_value={"provisioned": True}
        )
        mock_store_cls.return_value.async_save = AsyncMock()

        result = await async_ensure_hsem_dashboard(mock_hass)

    assert result["dashboard_url"] is None
    collection.async_create_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_dashboard_raises_when_collection_unavailable(
    mock_hass: MagicMock,
    tmp_path: Path,
    _bundled_yaml: Path,
) -> None:
    """A clear error is raised when the Lovelace collection is not loaded."""
    with (
        patch(
            "custom_components.hsem.utils.dashboard._active_dashboards_collection",
            return_value=None,
        ),
        pytest.raises(HomeAssistantError, match="Lovelace dashboard collection"),
    ):
        await async_ensure_hsem_dashboard(mock_hass)


@pytest.mark.asyncio
async def test_create_dashboard_uses_custom_path(
    mock_hass: MagicMock,
    tmp_path: Path,
    _bundled_yaml: Path,
) -> None:
    """A custom dashboard_path is honoured."""
    collection = MagicMock()
    collection.async_items.return_value = []
    collection.async_create_item = AsyncMock(return_value={"id": "dashboard-1"})

    config = MagicMock()
    config.async_save = AsyncMock()

    custom_path = tmp_path / "sub" / "custom_hsem.yaml"
    lovelace_data = MagicMock()
    lovelace_data.dashboards = {DASHBOARD_URL_PATH: config}
    mock_hass.data[LOVELACE_DATA] = lovelace_data

    with (
        patch(
            "custom_components.hsem.utils.dashboard._active_dashboards_collection",
            return_value=collection,
        ),
        patch(
            "custom_components.hsem.utils.dashboard.Store",
        ) as mock_store_cls,
    ):
        mock_store_cls.return_value.async_load = AsyncMock(return_value=None)
        mock_store_cls.return_value.async_save = AsyncMock()

        result = await async_ensure_hsem_dashboard(
            mock_hass, dashboard_path=custom_path
        )

    assert result["dashboard_path"] == str(custom_path)
    assert custom_path.exists()
