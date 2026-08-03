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


async def _get_or_create_collection(
    hass: HomeAssistant,
) -> lovelace_dashboard.DashboardsCollection:
    """Get or create a dashboards collection for HSEM use.

    This creates a new collection instance that reads from the same storage
    file as the main Lovelace integration. Changes made through this collection
    will be visible to the main integration after a reload.

    Args:
        hass: The Home Assistant instance.

    Returns:
        A loaded dashboards collection.
    """
    # Import lazily to avoid heavy HA component imports during unit tests.
    from homeassistant.components.lovelace import dashboard as lovelace_dashboard

    collection = lovelace_dashboard.DashboardsCollection(hass)
    await collection.async_load()
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


async def _trigger_dashboard_reload(hass: HomeAssistant) -> None:
    """Trigger a reload of the Lovelace dashboards.

    This sends a signal that causes the main Lovelace integration to reload
    its dashboards collection from storage, picking up any changes made
    by external collections.

    Args:
        hass: The Home Assistant instance.
    """
    # Fire an event that Lovelace listens to for dashboard changes
    hass.bus.async_fire("lovelace_dashboards_updated", {})
    _LOGGER.debug("Fired lovelace_dashboards_updated event")


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
        HomeAssistantError: When the Lovelace integration is not loaded, the
            bundled YAML is missing, or writing the dashboard fails.
    """
    destination = dashboard_path or _default_dashboard_path(hass)
    source = _bundled_dashboard_path()

    # Offload file I/O to the executor.
    await hass.async_add_executor_job(_write_dashboard_file_sync, source, destination)
    _LOGGER.info("HSEM dashboard YAML written to %s", destination)

    # Check if lovelace is loaded
    from homeassistant.components.lovelace.const import LOVELACE_DATA

    if hass.data.get(LOVELACE_DATA) is None:
        raise HomeAssistantError(
            "Lovelace integration is not loaded. "
            "Wait until Home Assistant has finished starting, then retry."
        )

    marker_store: Store[dict[str, bool]] = Store(
        hass,
        DASHBOARD_STORAGE_VERSION,
        DASHBOARD_STORAGE_KEY,
    )
    marker = await marker_store.async_load()

    # Create our own collection instance that reads from the same storage
    collection = await _get_or_create_collection(hass)
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

    # Create the dashboard item in the collection
    # This writes to the same storage file that Lovelace uses
    item = await collection.async_create_item(
        {
            "icon": DASHBOARD_ICON,
            "require_admin": False,
            "show_in_sidebar": True,
            "title": DASHBOARD_TITLE,
            "url_path": DASHBOARD_URL_PATH,
        }
    )
    _LOGGER.info("HSEM dashboard: created item with ID %s", item[CONF_ID])

    # The main Lovelace integration needs to reload its collection to see
    # the new dashboard. We can't directly trigger that, but the user can
    # reload the integration or restart HA.
    #
    # For now, we manually register the panel so it appears immediately.
    from homeassistant.components.lovelace import _register_panel

    _register_panel(
        hass,
        DASHBOARD_URL_PATH,
        "storage",
        {
            "url_path": DASHBOARD_URL_PATH,
            "title": DASHBOARD_TITLE,
            "icon": DASHBOARD_ICON,
            "require_admin": False,
            "show_in_sidebar": True,
        },
        False,
    )
    _LOGGER.info("Registered HSEM dashboard panel")

    # Now save the dashboard config
    try:
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is None:
            raise HomeAssistantError("Lovelace data is not available")

        # The config object is created by the panel registration listener
        # If it's not there yet, we create a LovelaceStorage directly
        config = lovelace_data.dashboards.get(DASHBOARD_URL_PATH)
        if config is None:
            from homeassistant.components.lovelace import (
                dashboard as lovelace_dashboard,
            )

            config = lovelace_dashboard.LovelaceStorage(hass, item)
            lovelace_data.dashboards[DASHBOARD_URL_PATH] = config

        # Parse the YAML so Home Assistant stores it as structured config.
        dashboard_config = yaml.safe_load(destination.read_text(encoding="utf-8"))
        await config.async_save(dashboard_config)
        _LOGGER.info("Saved dashboard config")
    except Exception as err:
        _LOGGER.error("Failed to save dashboard config: %s", err)
        # Don't roll back - the dashboard entry exists and the panel is registered
        # The user can manually import the YAML if needed

    await marker_store.async_save({"provisioned": True})
    _LOGGER.info("Created HSEM dashboard at URL /%s", DASHBOARD_URL_PATH)

    return {
        "dashboard_path": str(destination),
        "dashboard_url": f"/{DASHBOARD_URL_PATH}",
    }
