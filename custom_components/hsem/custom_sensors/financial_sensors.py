"""Financial sensors: export income, import cost, and net grid balance.

Three sensors that expose cumulative monetary totals accumulated across
coordinator cycles.  Export income and import cost are ``total_increasing``;
net grid balance is a ``measurement``.

State
-----
- ``sensor.hsem_export_income`` — cumulative export revenue (currency).
- ``sensor.hsem_import_cost`` — cumulative grid import cost (currency).
- ``sensor.hsem_net_grid_balance`` — export_income − import_cost (currency).

Attributes
----------
Each sensor exposes period attributes:
- ``today`` — today's contribution.
- ``last_7_days`` — sum over the last 7 calendar days.
- ``last_30_days`` — sum over the last 30 calendar days.
- ``this_month`` — sum over the current calendar month.
- ``this_year`` — sum over the current calendar year.
- ``daily`` — list of ``{date, import_cost, export_income, net_balance}`` records.
"""

from __future__ import annotations

from typing import Any, cast, override

from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor.const import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.restore_state import RestoreEntity

from custom_components.hsem.coordinator import (
    HSEMDataUpdateCoordinator,
)
from custom_components.hsem.entity import HSEMCoordinatorEntity, HSEMEntity
from custom_components.hsem.models.financial_tracker import FinancialTracker
from custom_components.hsem.utils.sensornames.financial import (
    get_export_income_entity_id,
    get_export_income_name,
    get_export_income_unique_id,
    get_import_cost_entity_id,
    get_import_cost_name,
    get_import_cost_unique_id,
    get_net_grid_balance_entity_id,
    get_net_grid_balance_name,
    get_net_grid_balance_unique_id,
)

# ---------------------------------------------------------------------------
# Shared mixin for financial sensors
# ---------------------------------------------------------------------------


class _FinancialSensorMixin(
    HSEMCoordinatorEntity,
    RestoreEntity,
    SensorEntity,
    HSEMEntity,
):
    """Common base for the three financial sensors.

    Provides the ``_get_tracker()`` helper and period attribute export.
    Subclasses set their own ``_attr_device_class``, ``_attr_state_class``,
    and override ``native_value``.
    """

    _attr_icon = "mdi:cash-multiple"
    _attr_has_entity_name = True

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise shared sensor state.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        HSEMCoordinatorEntity.__init__(self, coordinator)
        HSEMEntity.__init__(self, config_entry)
        self._config_entry = config_entry

    # ------------------------------------------------------------------
    # HA entity properties
    # ------------------------------------------------------------------

    @property
    @override
    def should_poll(self) -> bool:
        """No polling — driven by the coordinator."""
        return False

    @property
    @override
    def available(self) -> bool:
        """True once the coordinator has completed at least one successful cycle."""
        return self.coordinator.last_update_success

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return period rollup attributes from the financial tracker."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return cast(dict[str, Any], tracker.as_sensor_attributes())

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _get_tracker(self) -> FinancialTracker | None:
        """Return the financial tracker from the coordinator."""
        return getattr(self.coordinator, "_financial_tracker", None)


# ---------------------------------------------------------------------------
# Export Income Sensor
# ---------------------------------------------------------------------------


class HSEMExportIncomeSensor(_FinancialSensorMixin):
    """Cumulative export revenue sensor (total_increasing, monetary).

    State: total export income (currency).
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the export income sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_export_income_unique_id(config_entry.entry_id)
        self._attr_name = get_export_income_name()
        self.entity_id = get_export_income_entity_id()

    @property
    @override
    def native_value(self) -> float | None:
        """Return cumulative export income."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return round(tracker.export_income_total, 3)


# ---------------------------------------------------------------------------
# Import Cost Sensor
# ---------------------------------------------------------------------------


class HSEMImportCostSensor(_FinancialSensorMixin):
    """Cumulative grid import cost sensor (total_increasing, monetary).

    State: total grid import cost (currency).
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the import cost sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_import_cost_unique_id(config_entry.entry_id)
        self._attr_name = get_import_cost_name()
        self.entity_id = get_import_cost_entity_id()

    @property
    @override
    def native_value(self) -> float | None:
        """Return cumulative import cost."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return round(tracker.import_cost_total, 3)


# ---------------------------------------------------------------------------
# Net Grid Balance Sensor
# ---------------------------------------------------------------------------


class HSEMNetGridBalanceSensor(_FinancialSensorMixin):
    """Net grid balance sensor (measurement, monetary).

    State: cumulative export income minus cumulative import cost (currency).
    """

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        config_entry: ConfigEntry,
        coordinator: HSEMDataUpdateCoordinator,
    ) -> None:
        """Initialise the net grid balance sensor.

        Args:
            config_entry: The HSEM config entry.
            coordinator: The shared HSEM coordinator.
        """
        super().__init__(config_entry, coordinator)
        self._attr_unique_id = get_net_grid_balance_unique_id(config_entry.entry_id)
        self._attr_name = get_net_grid_balance_name()
        self.entity_id = get_net_grid_balance_entity_id()

    @property
    @override
    def native_value(self) -> float | None:
        """Return net grid balance (export income − import cost)."""
        tracker = self._get_tracker()
        if tracker is None:
            return None
        return round(tracker.export_income_total - tracker.import_cost_total, 3)
