"""Dashboard provisioning helper for HSEM.

Provides a single helper, :func:`async_ensure_hsem_dashboard`, that writes the
bundled HSEM Lovelace dashboard YAML to disk and registers a storage-mode
Lovelace dashboard so it appears in the HA sidebar.
"""

from __future__ import annotations

import os
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

# os.O_NOFOLLOW is POSIX-only; HSEM only runs under Home Assistant (Linux),
# but fall back to a no-op flag rather than raising on other platforms.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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
        return None

    handler_owner = getattr(registered[0], "__self__", None)
    collection = getattr(handler_owner, "storage_collection", None)
    if not isinstance(collection, lovelace_dashboard.DashboardsCollection):
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


def _resolve_contained_destination(hass: HomeAssistant, destination: Path) -> Path:
    """Resolve *destination* and confirm it stays inside the HA config directory.

    ``dashboard_path`` can arrive from the ``create_dashboard`` service call
    as an arbitrary caller-supplied string, so this rejects any path — via
    ``..`` segments or a symlinked parent directory — that would resolve
    outside the Home Assistant config directory, before any file I/O happens.

    Args:
        hass: The Home Assistant instance.
        destination: The requested (possibly unresolved) destination path.

    Returns:
        The resolved, contained destination path.

    Raises:
        HomeAssistantError: When *destination* resolves outside the config
            directory.
    """
    config_root = Path(hass.config.path()).resolve()
    resolved = destination.resolve()
    if resolved != config_root and config_root not in resolved.parents:
        raise HomeAssistantError(
            f"Dashboard path {destination} is outside the Home Assistant "
            "config directory."
        )
    return resolved


def _write_dashboard_file_sync(
    source_path: Path,
    destination_path: Path,
) -> None:
    """Copy the bundled dashboard YAML to *destination_path*.

    Synchronous I/O — must run inside the HA executor. *destination_path*
    must already be resolved and containment-checked (see
    :func:`_resolve_contained_destination`); this additionally opens it with
    ``O_NOFOLLOW`` so a symlink swapped in after that check is never
    followed for the write.

    Args:
        source_path: Path to the bundled YAML.
        destination_path: Path where the dashboard YAML should be written.

    Raises:
        HomeAssistantError: When the bundled YAML is missing or cannot be
            copied.
    """
    if not source_path.is_file() or source_path.is_symlink():
        raise HomeAssistantError(
            f"Bundled HSEM dashboard YAML not found at {source_path}"
        )
    content = source_path.read_text(encoding="utf-8")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            destination_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | _O_NOFOLLOW,
            0o644,
        )
    except OSError as err:
        raise HomeAssistantError(
            f"Cannot write dashboard YAML to {destination_path}: {err}"
        ) from err
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)


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
            bundled YAML is missing, ``dashboard_path`` resolves outside the
            Home Assistant config directory, or writing the dashboard fails.
    """
    destination = _resolve_contained_destination(
        hass, dashboard_path or _default_dashboard_path(hass)
    )
    source = _bundled_dashboard_path()

    # Offload file I/O to the executor.
    await hass.async_add_executor_job(_write_dashboard_file_sync, source, destination)

    collection = _active_dashboards_collection(hass)
    if collection is None:
        raise HomeAssistantError(
            "Lovelace dashboard collection is not available. "
            "Wait until Home Assistant has finished starting, then retry."
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

    try:
        lovelace_data = hass.data.get(LOVELACE_DATA)
        if lovelace_data is None:
            raise HomeAssistantError("Lovelace data is not available")

        config = lovelace_data.dashboards.get(DASHBOARD_URL_PATH)
        if config is None:
            raise HomeAssistantError(
                f"Lovelace dashboard config for /{DASHBOARD_URL_PATH} is missing"
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
