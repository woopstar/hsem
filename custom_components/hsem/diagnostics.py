"""Home Assistant diagnostics platform for HSEM.

Implements the HA ``async_get_config_entry_diagnostics`` hook so that users
can export a safe, redacted snapshot of the current HSEM state directly from
the Home Assistant UI (Settings → Devices & Services → HSEM → ... → Download
diagnostics).

The dump includes:
- The most recent planner input (battery hardware values, prices, PV forecast,
  consumption averages, schedule config) — suitable for reproducing the plan
  in the test suite via
  :func:`~custom_components.hsem.utils.diagnostics.load_planner_input_from_dump`.
- The selected plan (per-slot decisions, charge/discharge windows, explanation,
  rejected candidates).
- The hardware-write apply status from the most recent cycle.
- Integration version and dump timestamp.

All HA entity IDs and any field names that look like credentials are redacted
before the payload is returned so the output is safe to share in GitHub
issues.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.hsem.const import DOMAIN
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.utils.diagnostics import build_diagnostics_dump

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# HA diagnostics hook
# ---------------------------------------------------------------------------


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict:
    """Return a safe diagnostics dump for the given HSEM config entry.

    Called by Home Assistant when the user clicks *Download diagnostics* in
    the HA UI.  The returned dictionary is serialised to JSON by HA and
    offered as a file download.

    Args:
        hass: The running Home Assistant instance.
        entry: The HSEM config entry whose diagnostics are requested.

    Returns:
        A JSON-serialisable dictionary that is safe to share publicly.
    """
    domain_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator: HSEMDataUpdateCoordinator | None = domain_data.get("coordinator")

    if coordinator is None:
        _LOGGER.warning(
            "HSEM diagnostics requested but coordinator not found for entry %s",
            entry.entry_id,
        )
        return {
            "error": "coordinator_not_found",
            "entry_id": entry.entry_id,
        }

    # Retrieve the most recent planner input / output stored on the coordinator.
    planner_input = getattr(coordinator, "_last_planner_input", None)
    planner_output = getattr(coordinator, "_last_planner_output", None)
    apply_summary = coordinator.data.apply_summary if coordinator.data else None

    if planner_input is None or planner_output is None:
        _LOGGER.debug(
            "HSEM diagnostics: no planner cycle has completed yet for entry %s",
            entry.entry_id,
        )
        return {
            "error": "no_planner_cycle_completed",
            "entry_id": entry.entry_id,
        }

    try:
        from importlib.metadata import version as pkg_version

        integration_version = pkg_version("hsem")
    except Exception:  # noqa: BLE001
        integration_version = entry.version if hasattr(entry, "version") else "unknown"

    return build_diagnostics_dump(
        planner_input,
        planner_output,
        apply_summary,
        integration_version=str(integration_version),
    )
