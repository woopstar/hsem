"""Tests for the dashboard provisioning helpers and failure paths.

``test_dashboard.py`` covers the happy path and idempotence. These tests
cover the Lovelace collection lookup, the path-containment guard that keeps a
caller-supplied ``dashboard_path`` inside the config directory, the symlink-safe
write, and the rollback when saving the Lovelace config fails.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from jinja2 import Environment

from homeassistant.components import websocket_api
from homeassistant.components.lovelace import dashboard as lovelace_dashboard
from homeassistant.components.lovelace.const import (  # type: ignore[attr-defined]
    LOVELACE_DATA,
)
from homeassistant.exceptions import HomeAssistantError

from custom_components.hsem.utils.dashboard import (
    DASHBOARD_URL_PATH,
    _active_dashboards_collection,
    _bundled_dashboard_path,
    _resolve_contained_destination,
    _write_dashboard_file_sync,
    async_ensure_hsem_dashboard,
)

_MODULE = "custom_components.hsem.utils.dashboard"


def _find_multiple_entity_rows(value: Any) -> list[dict[str, Any]]:
    """Return every Multiple Entity Row configuration in nested YAML data."""
    if type(value) is dict:
        rows = [value] if value.get("type") == "custom:multiple-entity-row" else []
        for nested in value.values():
            rows.extend(_find_multiple_entity_rows(nested))
        return rows
    if type(value) is list:
        rows = []
        for nested in value:
            rows.extend(_find_multiple_entity_rows(nested))
        return rows
    return []


@pytest.fixture
def mock_hass(tmp_path: Path) -> MagicMock:
    """Return a mocked HomeAssistant with a config directory."""
    hass = MagicMock()
    hass.config.path.return_value = str(tmp_path)
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
    hass.data = {}
    return hass


@pytest.fixture
def bundled_yaml(tmp_path: Path) -> Generator[Path]:
    """Create a fake bundled dashboard YAML and patch the helper to use it."""
    dashboards_dir = tmp_path / "bundled"
    dashboards_dir.mkdir()
    source = dashboards_dir / "dashboard_en.yaml"
    source.write_text(
        yaml.safe_dump({"views": [{"title": "HSEM", "cards": []}]}), encoding="utf-8"
    )

    with patch(f"{_MODULE}._bundled_dashboard_path", return_value=source):
        yield source


class TestBundledDashboardPath:
    """The shipped dashboard YAML is found inside the integration."""

    def test_points_at_the_shipped_english_dashboard(self) -> None:
        """The bundled YAML exists in the installed integration."""
        path = _bundled_dashboard_path()

        assert path.name == "dashboard_en.yaml"
        assert path.is_file()

    def test_ev_economics_uses_dynamic_multiple_entity_rows(self) -> None:
        """Both EV cards group arbitrary deadline labels and round deltas."""
        dashboard_text = _bundled_dashboard_path().read_text(encoding="utf-8")

        assert yaml.safe_load(dashboard_text) is not None
        assert dashboard_text.count("type: custom:multiple-entity-row") == 2
        assert "title: EV Charging Economics" in dashboard_text
        assert "title: EV 2 Charging Economics" in dashboard_text
        assert dashboard_text.count("state: ready") >= 2
        assert dashboard_text.count("groupby('deadline_label')") == 2
        assert dashboard_text.count("p.delta_from_previous | round(2)") == 2
        assert dashboard_text.count("~ 'Next ' ~ label ~ ': '") == 2
        assert "selectattr('deadline_label', 'eq'" not in dashboard_text
        assert "white-space: pre-line" not in dashboard_text
        assert "output.lines | join" not in dashboard_text

    def test_ev_economics_template_has_clean_single_line_output(self) -> None:
        """Dynamic labels render without embedded newlines or indentation."""
        dashboard = yaml.safe_load(
            _bundled_dashboard_path().read_text(encoding="utf-8")
        )
        rows = _find_multiple_entity_rows(dashboard)
        template = rows[0]["entities"][0]["template"]
        points = [
            {
                "deadline_label": "09:30",
                "target_soc_pct": 80.0,
                "total_cost": 1.2345,
                "delta_from_previous": None,
                "feasible": True,
            },
            {
                "deadline_label": "09:30",
                "target_soc_pct": 100.0,
                "total_cost": 4.5678,
                "delta_from_previous": 3.3333,
                "feasible": False,
            },
            {
                "deadline_label": "22:15",
                "target_soc_pct": 100.0,
                "total_cost": 2.2222,
                "delta_from_previous": None,
                "feasible": True,
            },
        ]

        rendered = (
            Environment(autoescape=False)
            .from_string(template)
            .render(
                entity="sensor.hsem_ev_soc_economics",
                state_attr=lambda _entity, _attribute: points,
            )
        )

        assert rendered == (
            "Next 09:30: 80.0%: 1.2345 ✓  ·  "
            "100.0%: 4.5678 (+3.33) ⚠  |  "
            "Next 22:15: 100.0%: 2.2222 ✓"
        )
        assert "\n" not in rendered


class TestActiveDashboardsCollection:
    """The Lovelace collection is borrowed from the websocket handler."""

    def test_returns_the_storage_collection(self) -> None:
        """A registered handler exposes the live dashboards collection."""
        collection = MagicMock(spec=lovelace_dashboard.DashboardsCollection)
        handler_owner = MagicMock()
        handler_owner.storage_collection = collection
        handler = MagicMock(__self__=handler_owner)
        hass = MagicMock()
        hass.data = {websocket_api.DOMAIN: {"lovelace/dashboards/list": (handler,)}}

        assert _active_dashboards_collection(hass) is collection

    @pytest.mark.parametrize(
        "registered",
        [
            pytest.param({}, id="websocket_api_not_loaded"),
            pytest.param({websocket_api.DOMAIN: {}}, id="command_not_registered"),
            pytest.param(
                {websocket_api.DOMAIN: {"lovelace/dashboards/list": ()}},
                id="empty_registration",
            ),
            pytest.param(
                {websocket_api.DOMAIN: {"lovelace/dashboards/list": "not a tuple"}},
                id="unexpected_registration",
            ),
        ],
    )
    def test_unavailable_collection_is_reported_as_missing(
        self, registered: dict
    ) -> None:
        """Before Lovelace finishes loading there is no collection."""
        hass = MagicMock()
        hass.data = registered

        assert _active_dashboards_collection(hass) is None

    def test_handler_without_a_storage_collection_is_rejected(self) -> None:
        """A handler that is not the storage collection owner is ignored."""
        handler = MagicMock(__self__=MagicMock(storage_collection=object()))
        hass = MagicMock()
        hass.data = {websocket_api.DOMAIN: {"lovelace/dashboards/list": (handler,)}}

        assert _active_dashboards_collection(hass) is None


class TestPathContainment:
    """A caller-supplied dashboard path may not escape the config directory."""

    def test_path_inside_the_config_directory_is_accepted(
        self, mock_hass: MagicMock, tmp_path: Path
    ) -> None:
        """A normal path resolves and is returned."""
        destination = tmp_path / "sub" / "hsem_dashboard.yaml"

        assert _resolve_contained_destination(mock_hass, destination) == (
            destination.resolve()
        )

    def test_traversal_outside_the_config_directory_is_refused(
        self, mock_hass: MagicMock, tmp_path: Path
    ) -> None:
        """A ``..`` escape is rejected before any file I/O."""
        destination = tmp_path / ".." / "outside.yaml"

        with pytest.raises(HomeAssistantError, match="outside the Home Assistant"):
            _resolve_contained_destination(mock_hass, destination)

    @pytest.mark.asyncio
    async def test_service_call_with_an_escaping_path_is_refused(
        self, mock_hass: MagicMock, tmp_path: Path, bundled_yaml: Path
    ) -> None:
        """The guard runs before the dashboard file is written."""
        with pytest.raises(HomeAssistantError, match="outside the Home Assistant"):
            await async_ensure_hsem_dashboard(
                mock_hass, dashboard_path=tmp_path / ".." / "escaped.yaml"
            )

        assert not (tmp_path.parent / "escaped.yaml").exists()


class TestWriteDashboardFile:
    """The bundled YAML is copied without following symlinks."""

    def test_copies_the_bundled_yaml(self, tmp_path: Path, bundled_yaml: Path) -> None:
        """The destination receives the bundled content."""
        destination = tmp_path / "nested" / "hsem_dashboard.yaml"

        _write_dashboard_file_sync(bundled_yaml, destination)

        assert destination.read_text(encoding="utf-8") == bundled_yaml.read_text(
            encoding="utf-8"
        )

    def test_missing_bundled_yaml_is_an_error(self, tmp_path: Path) -> None:
        """A missing source is reported rather than silently skipped."""
        with pytest.raises(HomeAssistantError, match="not found"):
            _write_dashboard_file_sync(
                tmp_path / "absent.yaml", tmp_path / "hsem_dashboard.yaml"
            )

    def test_symlinked_source_is_refused(
        self, tmp_path: Path, bundled_yaml: Path
    ) -> None:
        """A symlinked source is not trusted."""
        link = tmp_path / "linked.yaml"
        link.symlink_to(bundled_yaml)

        with pytest.raises(HomeAssistantError, match="not found"):
            _write_dashboard_file_sync(link, tmp_path / "hsem_dashboard.yaml")

    def test_symlinked_destination_is_not_followed(
        self, tmp_path: Path, bundled_yaml: Path
    ) -> None:
        """A destination swapped for a symlink fails instead of writing through."""
        outside = tmp_path / "outside.yaml"
        outside.write_text("original", encoding="utf-8")
        destination = tmp_path / "hsem_dashboard.yaml"
        destination.symlink_to(outside)

        with pytest.raises(HomeAssistantError, match="Cannot write dashboard YAML"):
            _write_dashboard_file_sync(bundled_yaml, destination)

        assert outside.read_text(encoding="utf-8") == "original"


class TestLovelaceSaveFailures:
    """A failed Lovelace save must not leave a blank dashboard behind."""

    @staticmethod
    def _collection() -> MagicMock:
        """Return a dashboards collection with no existing HSEM dashboard."""
        collection = MagicMock()
        collection.async_items.return_value = []
        collection.async_create_item = AsyncMock(return_value={"id": "dashboard-1"})
        collection.async_delete_item = AsyncMock()
        return collection

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "lovelace_data",
        [
            pytest.param(None, id="lovelace_data_missing"),
            pytest.param(MagicMock(dashboards={}), id="dashboard_config_missing"),
        ],
    )
    async def test_missing_lovelace_config_rolls_back(
        self,
        mock_hass: MagicMock,
        bundled_yaml: Path,
        lovelace_data: MagicMock | None,
    ) -> None:
        """The freshly created dashboard entry is deleted again."""
        collection = self._collection()
        if lovelace_data is not None:
            mock_hass.data[LOVELACE_DATA] = lovelace_data

        with (
            patch(f"{_MODULE}._active_dashboards_collection", return_value=collection),
            patch(f"{_MODULE}.Store") as store_cls,
            pytest.raises(HomeAssistantError),
        ):
            store_cls.return_value.async_load = AsyncMock(return_value=None)
            store_cls.return_value.async_save = AsyncMock()
            await async_ensure_hsem_dashboard(mock_hass)

        collection.async_delete_item.assert_awaited_once_with("dashboard-1")

    @pytest.mark.asyncio
    async def test_failed_save_rolls_back_and_reraises(
        self, mock_hass: MagicMock, bundled_yaml: Path
    ) -> None:
        """A Lovelace save error propagates after the rollback."""
        collection = self._collection()
        config = MagicMock()
        config.async_save = AsyncMock(side_effect=HomeAssistantError("save failed"))
        mock_hass.data[LOVELACE_DATA] = MagicMock(
            dashboards={DASHBOARD_URL_PATH: config}
        )

        with (
            patch(f"{_MODULE}._active_dashboards_collection", return_value=collection),
            patch(f"{_MODULE}.Store") as store_cls,
            pytest.raises(HomeAssistantError, match="save failed"),
        ):
            store_cls.return_value.async_load = AsyncMock(return_value=None)
            store_cls.return_value.async_save = AsyncMock()
            await async_ensure_hsem_dashboard(mock_hass)

        collection.async_delete_item.assert_awaited_once_with("dashboard-1")
