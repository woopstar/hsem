"""Dashboard provisioning helper for HSEM.

Provides a single helper, :func:`async_ensure_hsem_dashboard`, that writes the
bundled HSEM Lovelace dashboard YAML to disk and registers a storage-mode
Lovelace dashboard so it appears in the HA sidebar.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from homeassistant.const import CONF_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

if TYPE_CHECKING:
    from homeassistant.components.lovelace import dashboard as lovelace_dashboard

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DASHBOARD_URL_PATH = "hsem-dashboard"
DASHBOARD_TITLE = "HSEM"
DASHBOARD_ICON = "mdi:solar-power"
DASHBOARD_STORAGE_VERSION = 1
DASHBOARD_STORAGE_KEY = f"{DOMAIN}.dashboard_provisioned"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _active_dashboards_collection(
    hass: HomeAssistant,
) -> lovelace_dashboard.DashboardsCollection | None:
    """Return the active Lovelace dashboards collection.

    Home Assistant does not expose a public Python API for creating storage
    dashboards. We retrieve the already-loaded collection via the websocket
    command's handler, which keeps storage file locking and panel registration
    in sync.

    Args:
        hass: The Home Assistant instance.

    Returns:
        The active dashboards collection, or ``None`` when it is not available
        (e.g. the ``lovelace`` integration has not finished loading).
    """
    # Import lazily to avoid heavy HA component imports during unit tests.
    from homeassistant.components import websocket_api
    from homeassistant.components.lovelace import dashboard as lovelace_dashboard

    registered = hass.data.get(websocket_api.DOMAIN, {}).get("lovelace/dashboards/list")
    if not isinstance(registered, tuple) or not registered:
        _LOGGER.warning(
            "HSEM dashboard: websocket command 'lovelace/dashboards/list' not "
            "registered (websocket_api keys present: %s) — is the lovelace "
            "integration loaded?",
            sorted(hass.data.get(websocket_api.DOMAIN, {}).keys()),
        )
        return None

    handler_owner = getattr(registered[0], "__self__", None)
    collection = getattr(handler_owner, "storage_collection", None)
    if not isinstance(collection, lovelace_dashboard.DashboardsCollection):
        _LOGGER.warning(
            "HSEM dashboard: unexpected handler owner for 'lovelace/dashboards/list': "
            "%r (has storage_collection: %s)",
            handler_owner,
            collection is not None,
        )
        return None

    return collection


def _find_existing_dashboard(
    items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return an existing HSEM dashboard from the collection, if any.

    Args:
        items: Dashboard collection items.

    Returns:
        The matching dashboard dict, or ``None``.
    """
    # Use string keys directly; importing the lovelace constants at module
    # level pulls in too many HA components and breaks unit tests.
    return next(
        (
            item
            for item in items
            if item.get("url_path") == DASHBOARD_URL_PATH
            or item.get("title") == DASHBOARD_TITLE
        ),
        None,
    )


def _default_dashboard_path(hass: HomeAssistant) -> Path:
    """Return the default file path for the HSEM dashboard YAML.

    Args:
        hass: The Home Assistant instance.

    Returns:
        Absolute path to ``<config>/hsem_dashboard.yaml``.
    """
    return Path(hass.config.path()) / "hsem_dashboard.yaml"


def _bundled_dashboard_path() -> Path:
    """Return the path to the bundled dashboard YAML shipped with HSEM."""
    return Path(__file__).parent.parent / "dashboards" / "dashboard_en.yaml"


def _write_dashboard_file_sync(
    source_path: Path,
    destination_path: Path,
) -> None:
    """Copy the bundled dashboard YAML to *destination_path*.

    Synchronous I/O — must run inside the HA executor.

    Args:
        source_path: Path to the bundled YAML.
        destination_path: Path where the dashboard YAML should be written.

    Raises:
        HomeAssistantError: When the bundled YAML is missing or cannot be
            copied.
    """
    if not source_path.exists():
        raise HomeAssistantError(
            f"Bundled HSEM dashboard YAML not found at {source_path}"
        )

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_text(
        source_path.read_text(encoding="utf-8"), encoding="utf-8"
    )


async def async_ensure_hsem_dashboard(
    hass: HomeAssistant,
    dashboard_path: Path | None = None,
) -> dict[str, Any]:
    """Ensure the HSEM Lovelace dashboard exists and points to the YAML file.

    The bundled dashboard YAML is copied to *dashboard_path* (default
    ``<config>/hsem_dashboard.yaml``). A storage-mode Lovelace dashboard is
    registered if it does not already exist. If the user previously deleted
    the dashboard via the UI, it is not recreated automatically.

    Args:
        hass: The Home Assistant instance.
        dashboard_path: Optional override for the destination YAML path.

    Returns:
        A dict with ``dashboard_path`` and ``dashboard_url`` keys.

    Raises:
        HomeAssistantError: When the Lovelace collection is unavailable, the
            bundled YAML is missing, or writing the dashboard fails.
    """
    destination = dashboard_path or _default_dashboard_path(hass)
    source = _bundled_dashboard_path()

    # Offload file I/O to the executor.
    await hass.async_add_executor_job(_write_dashboard_file_sync, source, destination)
    _LOGGER.info("HSEM dashboard YAML written to %s", destination)

    collection = _active_dashboards_collection(hass)
    if collection is None:
        raise HomeAssistantError(
            "Lovelace dashboard collection is not available — the dashboard "
            "was written to disk but could not be registered in Home "
            "Assistant. Ensure the lovelace integration is loaded, then retry."
        )

    _LOGGER.debug(
        "HSEM dashboard: Lovelace collection found, %d existing dashboard(s)",
        len(collection.async_items()),
    )

    marker_store: Store[dict[str, bool]] = Store(
        hass,
        DASHBOARD_STORAGE_VERSION,
        DASHBOARD_STORAGE_KEY,
    )
    marker = await marker_store.async_load()
    existing = _find_existing_dashboard(collection.async_items())

    if existing is not None:
        _LOGGER.info("HSEM dashboard already exists at URL /%s", DASHBOARD_URL_PATH)
        if not marker:
            await marker_store.async_save({"provisioned": True})
        return {
            "dashboard_path": str(destination),
            "dashboard_url": f"/{DASHBOARD_URL_PATH}",
        }

    # A retained marker with no matching dashboard means the user deleted it
    # deliberately. Do not recreate it.
    if marker and marker.get("provisioned"):
        _LOGGER.info(
            "HSEM dashboard was previously deleted by the user; not recreating"
        )
        return {
            "dashboard_path": str(destination),
            "dashboard_url": None,
        }

    # Import lazily to avoid heavy HA component imports during unit tests.
    from homeassistant.components.lovelace.const import (
        LOVELACE_DATA,  # type: ignore[attr-defined]
    )

    item = await collection.async_create_item(
        {
            "icon": DASHBOARD_ICON,
            "require_admin": False,
            "show_in_sidebar": True,
            "title": DASHBOARD_TITLE,
            "url_path": DASHBOARD_URL_PATH,
        }
    )
    _LOGGER.debug("HSEM dashboard: collection item created: %s", item)

    try:
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is None:
            raise HomeAssistantError("Lovelace data is not available")

        config = lovelace_data.dashboards.get(DASHBOARD_URL_PATH)
        if config is None:
            raise HomeAssistantError(
                "Lovelace dashboard config for "
                f"/{DASHBOARD_URL_PATH} is missing after creation — "
                "the panel listener may not have run yet. Retry the service."
            )

        # Parse the YAML so Home Assistant stores it as structured config.
        dashboard_config = yaml.safe_load(destination.read_text(encoding="utf-8"))
        await config.async_save(dashboard_config)
    except Exception:
        # Roll back the created dashboard so we do not leave a blank entry.
        await collection.async_delete_item(item[CONF_ID])
        raise

    await marker_store.async_save({"provisioned": True})
    _LOGGER.info("Created HSEM dashboard at URL /%s", DASHBOARD_URL_PATH)

    return {
        "dashboard_path": str(destination),
        "dashboard_url": f"/{DASHBOARD_URL_PATH}",
    }
