"""Config-entry reader for HSEMWorkingModeSensor.

Single responsibility: read all config-entry options and convert them into
a typed :class:`~custom_components.hsem.models.sensor_config.SensorConfig`
dataclass.

This module has **no** Home Assistant entity I/O — it only reads
``config_entry.options`` via :func:`get_config_value`.  It can therefore be
called synchronously and tested without a running HA instance.
"""

from __future__ import annotations

import voluptuous as vol

from custom_components.hsem.models.battery_schedule import BatterySchedule
from custom_components.hsem.models.sensor_config import (
    BatteryScheduleConfig,
    EVChargerConfig,
    SensorConfig,
)
from custom_components.hsem.utils.misc import (
    convert_months_to_int,
    convert_to_boolean,
    convert_to_float,
    convert_to_int,
    convert_to_time,
    get_config_value,
)


def build_sensor_config(config_entry) -> SensorConfig:
    """Read all config-entry options and return a populated :class:`SensorConfig`.

    This is a pure synchronous function that reads from ``config_entry.options``
    (or ``.data``) via :func:`get_config_value`.  It performs no I/O and can be
    called from tests with a mock config entry.

    Args:
        config_entry: A Home Assistant ``ConfigEntry`` or any object that
            :func:`get_config_value` accepts.

    Returns:
        A fully populated :class:`SensorConfig`.
    """
    cfg = SensorConfig()

    cfg.read_only = convert_to_boolean(get_config_value(config_entry, "hsem_read_only"))
    cfg.verbose_logging = convert_to_boolean(
        get_config_value(config_entry, "hsem_verbose_logging")
    )
    cfg.extended_attributes = convert_to_boolean(
        get_config_value(config_entry, "hsem_extended_attributes")
    )
    cfg.update_interval = convert_to_int(
        get_config_value(config_entry, "hsem_update_interval")
    )
    cfg.recommendation_interval_minutes = convert_to_int(
        get_config_value(config_entry, "hsem_recommendation_interval_minutes")
    )
    cfg.recommendation_interval_length = convert_to_int(
        get_config_value(config_entry, "hsem_recommendation_interval_length")
    )
    cfg.energi_data_service_update_interval = convert_to_int(
        get_config_value(config_entry, "hsem_energi_data_service_update_interval")
    )

    # Seasonal months
    months_winter = get_config_value(config_entry, "hsem_months_winter")
    months_summer = get_config_value(config_entry, "hsem_months_summer")
    cfg.months_winter = (
        convert_months_to_int(months_winter) if isinstance(months_winter, list) else []
    )
    cfg.months_summer = (
        convert_months_to_int(months_summer) if isinstance(months_summer, list) else []
    )

    # Huawei Solar device IDs
    cfg.huawei_solar_device_id_inverter_1 = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_inverter_1"
    )
    cfg.huawei_solar_device_id_inverter_2 = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_inverter_2"
    )
    if (
        cfg.huawei_solar_device_id_inverter_2 is not None
        and len(cfg.huawei_solar_device_id_inverter_2) == 0
    ):
        cfg.huawei_solar_device_id_inverter_2 = None

    cfg.huawei_solar_device_id_batteries = get_config_value(
        config_entry, "hsem_huawei_solar_device_id_batteries"
    )

    # Huawei Solar entity IDs
    cfg.huawei_solar_batteries_working_mode = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_working_mode"
    )
    cfg.huawei_solar_batteries_end_of_discharge_soc = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_end_of_discharge_soc"
    )
    cfg.huawei_solar_batteries_state_of_capacity = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_state_of_capacity"
    )
    cfg.huawei_solar_batteries_grid_charge_cutoff_soc = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_grid_charge_cutoff_soc"
    )
    cfg.huawei_solar_batteries_maximum_charging_power = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_maximum_charging_power"
    )
    cfg.huawei_solar_batteries_maximum_discharging_power = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_maximum_discharging_power"
    )
    cfg.huawei_solar_batteries_tou_charging_and_discharging_periods = get_config_value(
        config_entry,
        "hsem_huawei_solar_batteries_tou_charging_and_discharging_periods",
    )
    cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou"
    )
    cfg.huawei_solar_inverter_active_power_control = get_config_value(
        config_entry, "hsem_huawei_solar_inverter_active_power_control"
    )
    cfg.huawei_solar_batteries_rated_capacity = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_rated_capacity"
    )

    # Power meters
    cfg.house_consumption_power = get_config_value(
        config_entry, "hsem_house_consumption_power"
    )
    cfg.solar_production_power = get_config_value(
        config_entry, "hsem_solar_production_power"
    )
    cfg.house_power_includes_ev_charger_power = convert_to_boolean(
        get_config_value(config_entry, "hsem_house_power_includes_ev_charger_power")
    )

    # Solcast
    cfg.solcast_pv_forecast_forecast_today = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_today"
    )
    cfg.solcast_pv_forecast_forecast_tomorrow = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_tomorrow"
    )
    cfg.solcast_pv_forecast_forecast_likelihood = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_likelihood"
    )

    # Energi Data Service
    cfg.energi_data_service_import = get_config_value(
        config_entry, "hsem_energi_data_service_import"
    )
    cfg.energi_data_service_export = get_config_value(
        config_entry, "hsem_energi_data_service_export"
    )
    cfg.energi_data_service_export_min_price = convert_to_float(
        get_config_value(config_entry, "hsem_energi_data_service_export_min_price")
    )

    # First EV charger
    ev = EVChargerConfig()
    ev.status_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_charger_status")
    )
    ev.power_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_charger_power")
    )
    ev.soc_entity = _optional_entity(get_config_value(config_entry, "hsem_ev_soc"))
    ev.soc_target_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_soc_target")
    )
    ev.connected_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_connected")
    )
    ev.allow_charge_past_target_soc = convert_to_boolean(
        get_config_value(config_entry, "hsem_ev_allow_charge_past_target_soc")
    )
    ev.force_max_discharge_power = convert_to_boolean(
        get_config_value(config_entry, "hsem_ev_charger_force_max_discharge_power")
    )
    ev.max_discharge_power = convert_to_int(
        get_config_value(config_entry, "hsem_ev_charger_max_discharge_power")
    )
    cfg.ev = ev

    # Second EV charger
    ev2 = EVChargerConfig()
    ev2.status_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_second_charger_status")
    )
    ev2.power_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_second_charger_power")
    )
    ev2.soc_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_second_soc")
    )
    ev2.soc_target_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_second_soc_target")
    )
    ev2.connected_entity = _optional_entity(
        get_config_value(config_entry, "hsem_ev_second_connected")
    )
    ev2.allow_charge_past_target_soc = convert_to_boolean(
        get_config_value(config_entry, "hsem_ev_second_allow_charge_past_target_soc")
    )
    ev2.force_max_discharge_power = convert_to_boolean(
        get_config_value(
            config_entry, "hsem_ev_second_charger_force_max_discharge_power"
        )
    )
    ev2.max_discharge_power = convert_to_int(
        get_config_value(config_entry, "hsem_ev_second_charger_max_discharge_power")
    )
    cfg.ev_second = ev2

    # Battery economics
    cfg.batteries_conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss")
    )
    cfg.batteries_purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price")
    )
    cfg.batteries_expected_cycles = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
    )

    # Battery schedules
    cfg.batteries_schedule_1 = BatteryScheduleConfig(
        enabled=convert_to_boolean(
            get_config_value(config_entry, "hsem_batteries_enable_batteries_schedule_1")
        ),
        start=convert_to_time(
            get_config_value(
                config_entry, "hsem_batteries_enable_batteries_schedule_1_start"
            )
        ),
        end=convert_to_time(
            get_config_value(
                config_entry, "hsem_batteries_enable_batteries_schedule_1_end"
            )
        ),
        min_price_difference=convert_to_float(
            get_config_value(
                config_entry,
                "hsem_batteries_enable_batteries_schedule_1_min_price_difference",
            )
        ),
    )
    cfg.batteries_schedule_2 = BatteryScheduleConfig(
        enabled=convert_to_boolean(
            get_config_value(config_entry, "hsem_batteries_enable_batteries_schedule_2")
        ),
        start=convert_to_time(
            get_config_value(
                config_entry, "hsem_batteries_enable_batteries_schedule_2_start"
            )
        ),
        end=convert_to_time(
            get_config_value(
                config_entry, "hsem_batteries_enable_batteries_schedule_2_end"
            )
        ),
        min_price_difference=convert_to_float(
            get_config_value(
                config_entry,
                "hsem_batteries_enable_batteries_schedule_2_min_price_difference",
            )
        ),
    )
    cfg.batteries_schedule_3 = BatteryScheduleConfig(
        enabled=convert_to_boolean(
            get_config_value(config_entry, "hsem_batteries_enable_batteries_schedule_3")
        ),
        start=convert_to_time(
            get_config_value(
                config_entry, "hsem_batteries_enable_batteries_schedule_3_start"
            )
        ),
        end=convert_to_time(
            get_config_value(
                config_entry, "hsem_batteries_enable_batteries_schedule_3_end"
            )
        ),
        min_price_difference=convert_to_float(
            get_config_value(
                config_entry,
                "hsem_batteries_enable_batteries_schedule_3_min_price_difference",
            )
        ),
    )

    # Excess export
    cfg.batteries_enable_excess_export = get_config_value(
        config_entry, "hsem_batteries_enable_excess_export"
    )
    cfg.batteries_excess_export_discharge_buffer = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_excess_export_discharge_buffer")
    )
    cfg.batteries_excess_export_price_threshold = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_excess_export_price_threshold")
    )

    # Consumption weights
    cfg.house_consumption_energy_weight_1d = convert_to_int(
        get_config_value(config_entry, "hsem_house_consumption_energy_weight_1d")
    )
    cfg.house_consumption_energy_weight_3d = convert_to_int(
        get_config_value(config_entry, "hsem_house_consumption_energy_weight_3d")
    )
    cfg.house_consumption_energy_weight_7d = convert_to_int(
        get_config_value(config_entry, "hsem_house_consumption_energy_weight_7d")
    )
    cfg.house_consumption_energy_weight_14d = convert_to_int(
        get_config_value(config_entry, "hsem_house_consumption_energy_weight_14d")
    )

    return cfg


def build_battery_schedules(cfg: SensorConfig) -> list[BatterySchedule]:
    """Convert the three :class:`BatteryScheduleConfig` objects into :class:`BatterySchedule` instances.

    Args:
        cfg: Populated sensor configuration.

    Returns:
        A list of three :class:`BatterySchedule` objects (always three, regardless
        of whether they are enabled).
    """
    schedules = []
    for sc in cfg.schedule_configs():
        schedules.append(
            BatterySchedule(
                enabled=sc.enabled,
                start=sc.start,
                end=sc.end,
                avg_import_price=0.0,
                needed_batteries_capacity=0.0,
                needed_batteries_capacity_cost=0.0,
                min_price_difference_required=sc.min_price_difference,
            )
        )
    return schedules


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _optional_entity(value) -> str | None:
    """Return None if value is vol.UNDEFINED or falsy, else the string."""
    if value is vol.UNDEFINED or not value:
        return None
    return value
