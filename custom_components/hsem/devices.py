"""Per-subsystem device grouping for the HSEM integration (issue #875).

Historically every HSEM entity was attached to a single Home Assistant
device (``DeviceInfo(identifiers={(DOMAIN, entry_id)}, ...)``). With ~220+
entities that made the device page unwieldy and impossible to scope
automations/dashboards or HA Areas around one subsystem.

This module defines the 7 devices entities can now be attached to, plus a
:func:`get_device_info` dispatcher used by :class:`HSEMEntity.device_info`.
``HSEMDevice.CONTROLLER`` keeps the original ``(DOMAIN, entry_id)``
identifier so entities that stay on it need no migration; every other
device gets a new ``(DOMAIN, f"{entry_id}_{device}")`` identifier and
requires the one-time entity-registry migration in
:mod:`custom_components.hsem.device_migration`.
"""

from __future__ import annotations

from enum import StrEnum

from homeassistant.helpers.device_registry import DeviceInfo

from custom_components.hsem.const import DOMAIN, NAME


class HSEMDevice(StrEnum):
    """Identifies which of the 7 HSEM devices an entity is attached to."""

    CONTROLLER = "controller"
    BATTERY_ENERGY = "battery_energy"
    HOURLY_CONSUMPTION = "hourly_consumption"
    FINANCIAL = "financial"
    FORECAST = "forecast"
    EV_PRIMARY = "ev_primary"
    EV_SECONDARY = "ev_secondary"


#: Display name for each device. ``CONTROLLER`` keeps the bare integration
#: name — it is the pre-split single device and needs no visual change.
_DEVICE_NAMES: dict[HSEMDevice, str] = {
    HSEMDevice.CONTROLLER: NAME,
    HSEMDevice.BATTERY_ENERGY: f"{NAME} Battery & Energy",
    HSEMDevice.HOURLY_CONSUMPTION: f"{NAME} Hourly Consumption Profile",
    HSEMDevice.FINANCIAL: f"{NAME} Financial",
    HSEMDevice.FORECAST: f"{NAME} Forecast",
    HSEMDevice.EV_PRIMARY: f"{NAME} EV Primary",
    HSEMDevice.EV_SECONDARY: f"{NAME} EV Secondary",
}


def get_device_identifier(entry_id: str, device: HSEMDevice) -> str:
    """Return the device-registry identifier suffix for ``device``.

    ``CONTROLLER`` reuses the bare ``entry_id`` identifier that predates
    the device split so its entities never need a migration. Every other
    device gets a stable ``f"{entry_id}_{device.value}"`` identifier.
    """
    if device is HSEMDevice.CONTROLLER:
        return entry_id
    return f"{entry_id}_{device.value}"


def get_device_info(entry_id: str, device: HSEMDevice) -> DeviceInfo:
    """Return the :class:`DeviceInfo` for one of the 7 HSEM devices."""
    return DeviceInfo(
        identifiers={(DOMAIN, get_device_identifier(entry_id, device))},
        name=_DEVICE_NAMES[device],
        manufacturer=DOMAIN.upper(),
        model="Custom Integration",
    )
