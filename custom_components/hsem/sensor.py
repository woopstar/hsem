"""Sensor platform for the HSEM integration.

Sets up all HSEM sensor entities from a config entry, including
diagnostic sensors, forecast accuracy, working mode, and EV charging
sensors.  All sensors subscribe to the shared
:class:`HSEMDataUpdateCoordinator` for periodic updates.
"""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from custom_components.hsem import HSEMConfigEntry
from custom_components.hsem.coordinator import HSEMDataUpdateCoordinator
from custom_components.hsem.custom_sensors.applier_status_sensor import (
    HSEMApplierStatusSensor,
)
from custom_components.hsem.custom_sensors.battery_soc_sensor import (
    HSEMBatterySoCSensor,
)
from custom_components.hsem.custom_sensors.daily_plan_vs_actual_sensor import (
    HSEMDailyPlanVsActualSensor,
)
from custom_components.hsem.custom_sensors.degraded_mode_sensor import (
    HSEMDegradedModeSensor,
)
from custom_components.hsem.custom_sensors.effective_discharge_floor_sensor import (
    HSEMEffectiveDischargeFloorSensor,
)
from custom_components.hsem.custom_sensors.ev_charging_sensor import (
    HSEMEVChargingSensor,
)
from custom_components.hsem.custom_sensors.ev_optimal_charging_plan_sensor import (
    HSEMEVOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.ev_second_optimal_charging_plan_sensor import (
    HSEMEVSecondOptimalChargingPlanSensor,
)
from custom_components.hsem.custom_sensors.financial_sensors import (
    HSEMExportIncomeSensor,
    HSEMImportCostSensor,
    HSEMNetGridBalanceSensor,
)
from custom_components.hsem.custom_sensors.force_mode_sensor import HSEMForceModeSensor
from custom_components.hsem.custom_sensors.forecast_accuracy_sensor import (
    HSEMForecastAccuracySensor,
)
from custom_components.hsem.custom_sensors.hardware_writes_sensor import (
    HSEMHardwareWritesSensor,
)
from custom_components.hsem.custom_sensors.house_consumption_power_sensor import (
    HSEMHouseConsumptionPowerSensor,
)
from custom_components.hsem.custom_sensors.last_updated_sensor import (
    HSEMLastUpdatedSensor,
)
from custom_components.hsem.custom_sensors.missing_entities_sensor import (
    HSEMMissingEntitiesSensor,
)
from custom_components.hsem.custom_sensors.net_consumption_sensor import (
    HSEMNetConsumptionSensor,
)
from custom_components.hsem.custom_sensors.next_update_sensor import (
    HSEMNextUpdateSensor,
)
from custom_components.hsem.custom_sensors.ocpp_sensors import (
    HSEMOCPPChargerInfoSensor,
    HSEMOCPPChargerPowerSensor,
    HSEMOCPPChargerSessionsSensor,
    HSEMOCPPChargerStatusSensor,
)
from custom_components.hsem.custom_sensors.plan_explanation_sensor import (
    HSEMPlanExplanationSensor,
)
from custom_components.hsem.custom_sensors.prediction_accuracy_sensor import (
    HSEMPredictionAccuracySensor,
)
from custom_components.hsem.custom_sensors.pv_curtailment_sensor import (
    HSEMPVTailedSensor,
)
from custom_components.hsem.custom_sensors.read_only_sensor import HSEMReadOnlySensor
from custom_components.hsem.custom_sensors.recommendation_interval_sensor import (
    HSEMRecommendationIntervalSensor,
)
from custom_components.hsem.custom_sensors.savings_sensor import (
    HSEMSavingsSensor,
)
from custom_components.hsem.custom_sensors.solar_confidence_sensor import (
    HSEMSolarConfidenceSensor,
)
from custom_components.hsem.custom_sensors.update_interval_sensor import (
    HSEMUpdateIntervalSensor,
)
from custom_components.hsem.custom_sensors.working_mode_sensor import (
    HSEMWorkingModeSensor,
)


async def async_setup_entry(  # NOSONAR -- HA platform callback, must be async
    hass: HomeAssistant,
    config_entry: HSEMConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up HSEM sensors from a config entry."""

    # Retrieve the coordinator from runtime_data (Bronze rule: runtime-data).
    coordinator: HSEMDataUpdateCoordinator = config_entry.runtime_data.coordinator

    # Diagnostic sensors — all subscribe to coordinator updates.
    degraded_mode_sensor = HSEMDegradedModeSensor(config_entry, coordinator)
    read_only_sensor = HSEMReadOnlySensor(config_entry, coordinator)
    next_update_sensor = HSEMNextUpdateSensor(config_entry, coordinator)
    last_updated_sensor = HSEMLastUpdatedSensor(config_entry, coordinator)
    update_interval_sensor = HSEMUpdateIntervalSensor(config_entry, coordinator)
    recommendation_interval_sensor = HSEMRecommendationIntervalSensor(
        config_entry, coordinator
    )
    missing_entities_sensor = HSEMMissingEntitiesSensor(config_entry, coordinator)
    hardware_writes_sensor = HSEMHardwareWritesSensor(config_entry, coordinator)
    net_consumption_sensor = HSEMNetConsumptionSensor(config_entry, coordinator)
    battery_soc_sensor = HSEMBatterySoCSensor(config_entry, coordinator)
    force_mode_sensor = HSEMForceModeSensor(config_entry, coordinator)
    ev_charging_sensor = HSEMEVChargingSensor(config_entry, coordinator)
    ev_optimal_charging_plan_sensor = HSEMEVOptimalChargingPlanSensor(
        config_entry, coordinator
    )
    ev_second_optimal_charging_plan_sensor = HSEMEVSecondOptimalChargingPlanSensor(
        config_entry, coordinator
    )

    # Forecast accuracy sensor — exposes predicted-vs-actual metrics.
    forecast_accuracy_sensor = HSEMForecastAccuracySensor(config_entry, coordinator)

    # Solar confidence sensor — exposes per-hour PV forecast accuracy factors.
    solar_confidence_sensor = HSEMSolarConfidenceSensor(config_entry, coordinator)

    # Prediction accuracy sensor — SoC MAE, solar MAPE, action mix scorecard.
    prediction_accuracy_sensor = HSEMPredictionAccuracySensor(config_entry, coordinator)

    # PV curtailment sensor — detects when inverter throttles solar production.
    pv_curtailment_sensor = HSEMPVTailedSensor(config_entry, coordinator)

    # Working-mode sensor — subscribes to coordinator updates and owns hardware writes.
    working_mode_sensor = HSEMWorkingModeSensor(config_entry, coordinator)

    # Applier-status sensor — exposes write-and-verify results per cycle.
    applier_status_sensor = HSEMApplierStatusSensor(config_entry, coordinator)

    # Plan-explanation sensor — exposes the active planner strategy and score.
    plan_explanation_sensor = HSEMPlanExplanationSensor(config_entry, coordinator)

    # Daily plan-vs-actual sensor — exposes cumulative daily and historical metrics.
    daily_plan_vs_actual_sensor = HSEMDailyPlanVsActualSensor(config_entry, coordinator)

    # Effective discharge floor sensor — dynamic floor diagnostics.
    effective_discharge_floor_sensor = HSEMEffectiveDischargeFloorSensor(
        config_entry, coordinator
    )

    # Financial sensors — export income, import cost, and net grid balance.
    export_income_sensor = HSEMExportIncomeSensor(config_entry, coordinator)
    import_cost_sensor = HSEMImportCostSensor(config_entry, coordinator)
    net_grid_balance_sensor = HSEMNetGridBalanceSensor(config_entry, coordinator)

    # Savings tracker sensor — exposes actual vs missed savings metrics.
    savings_sensor = HSEMSavingsSensor(config_entry, coordinator)

    # OCPP charger sensors — expose charger state when OCPP server is enabled.
    ocpp_charger_status_sensor = HSEMOCPPChargerStatusSensor(config_entry, coordinator)
    ocpp_charger_power_sensor = HSEMOCPPChargerPowerSensor(config_entry, coordinator)
    ocpp_charger_info_sensor = HSEMOCPPChargerInfoSensor(config_entry, coordinator)
    ocpp_charger_sessions_sensor = HSEMOCPPChargerSessionsSensor(
        config_entry, coordinator
    )

    async_add_entities(
        [
            degraded_mode_sensor,
            read_only_sensor,
            next_update_sensor,
            last_updated_sensor,
            update_interval_sensor,
            recommendation_interval_sensor,
            missing_entities_sensor,
            hardware_writes_sensor,
            net_consumption_sensor,
            battery_soc_sensor,
            force_mode_sensor,
            ev_charging_sensor,
            ev_optimal_charging_plan_sensor,
            ev_second_optimal_charging_plan_sensor,
            applier_status_sensor,
            plan_explanation_sensor,
            forecast_accuracy_sensor,
            solar_confidence_sensor,
            prediction_accuracy_sensor,
            pv_curtailment_sensor,
            daily_plan_vs_actual_sensor,
            effective_discharge_floor_sensor,
            savings_sensor,
            export_income_sensor,
            import_cost_sensor,
            net_grid_balance_sensor,
            working_mode_sensor,
            ocpp_charger_status_sensor,
            ocpp_charger_power_sensor,
            ocpp_charger_info_sensor,
            ocpp_charger_sessions_sensor,
        ]
    )

    # Add power, energy and energy average sensors (these remain self-polling).
    power_sensors = []
    for hour in range(24):
        hour_start = hour
        hour_end = (hour + 1) % 24
        sensor = HSEMHouseConsumptionPowerSensor(
            config_entry, hour_start, hour_end, async_add_entities
        )
        power_sensors.append(sensor)

    # Add sensors to Home Assistant
    async_add_entities(power_sensors)
