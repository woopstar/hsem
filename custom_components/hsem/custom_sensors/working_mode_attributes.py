"""Planning-snapshot attributes for the HSEM working-mode sensor.

Split out of ``working_mode_sensor.py`` to keep that entity under the
repository's 30 KB file limit.  Pure dict assembly from one
:class:`~custom_components.hsem.coordinator_data.CoordinatorData` snapshot.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from custom_components.hsem.coordinator_data import CoordinatorData
from custom_components.hsem.custom_sensors.applier_caps import (
    wait_mode_self_consumption_surplus_kwh,
)
from custom_components.hsem.models.live_state import LiveState
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.utils.misc import (
    calculate_recommended_threshold,
    get_config_value,
)


def _wait_mode_self_consumption(
    data: CoordinatorData,
    cfg: SensorConfig,
    live: LiveState,
) -> dict[str, Any]:
    """Return what the live wait slot does with the battery (issue #1255).

    The plan books no discharge on a ``batteries_wait_mode`` slot, so its SoC
    estimate is flat, while ``self_consumption_with_reserve`` lets the house
    use the energy above the reserve.  This says which of the two the applier
    executes, from the same helper the applier reads.
    """
    rec = data.hourly_recommendation
    surplus_kwh = (
        wait_mode_self_consumption_surplus_kwh(
            cfg, live, rec, data.current_wait_mode_reserve
        )
        if rec is not None
        else None
    )
    if surplus_kwh is None:
        return {"active": False, "reserve_kwh": None, "surplus_kwh": None}
    return {
        "active": surplus_kwh > 1e-9,
        "reserve_kwh": round(data.current_wait_mode_reserve or 0.0, 3),
        "surplus_kwh": round(surplus_kwh, 3),
    }


def build_working_mode_attributes(
    data: CoordinatorData,
    cfg: SensorConfig,
    live: LiveState,
    *,
    unique_id: str | None,
    config_entry: ConfigEntry,
    primary_grid_charge_owned: bool,
) -> dict[str, Any]:
    """Return the planning, config, and live-state attributes.

    Args:
        data: Coordinator snapshot being published.
        cfg: Its resolved sensor configuration.
        live: Its live state.
        unique_id: The working-mode sensor's unique ID.
        config_entry: The HSEM config entry (for per-EV force-charge flags).
        primary_grid_charge_owned: Whether HSEM owns the primary grid charge.

    Returns:
        Attribute dict; extended entity-ID attributes are included when
        ``cfg.extended_attributes`` is set.
    """
    extended = {}
    if cfg.extended_attributes:
        extended = {
            "import_electricity_price_sensor_entity": cfg.import_electricity_price_sensor,
            "export_electricity_price_sensor_entity": cfg.export_electricity_price_sensor,
            "ev_charger_power_entity": cfg.ev.power_entity,
            "ev_charger_status_entity": cfg.ev.status_entity,
            "ev_soc_entity": cfg.ev.soc_entity,
            "ev_connected_entity": cfg.ev.connected_entity,
            "ev_second_charger_power_entity": cfg.ev_second.power_entity,
            "ev_second_charger_status_entity": cfg.ev_second.status_entity,
            "ev_second_soc_entity": cfg.ev_second.soc_entity,
            "ev_second_connected_entity": cfg.ev_second.connected_entity,
            "force_working_mode_entity": live.force_working_mode,
            "house_consumption_power_entity": cfg.house_consumption_power,
            "hsem_huawei_solar_batteries_end_of_discharge_soc_entity": cfg.huawei_solar_batteries_end_of_discharge_soc,
            "huawei_solar_batteries_grid_charge_cutoff_soc_entity": cfg.huawei_solar_batteries_grid_charge_cutoff_soc,
            "huawei_solar_batteries_maximum_charging_power_entity": cfg.huawei_solar_batteries_maximum_charging_power,
            "huawei_solar_batteries_maximum_discharging_power_entity": cfg.huawei_solar_batteries_maximum_discharging_power,
            "huawei_solar_batteries_grid_charge_maximum_power_entity": cfg.huawei_solar_batteries_grid_charge_maximum_power,
            "huawei_solar_power_meter_phase_a_active_power_entity": cfg.huawei_solar_power_meter_phase_a_active_power,
            "huawei_solar_power_meter_phase_b_active_power_entity": cfg.huawei_solar_power_meter_phase_b_active_power,
            "huawei_solar_power_meter_phase_c_active_power_entity": cfg.huawei_solar_power_meter_phase_c_active_power,
            "huawei_solar_batteries_rated_capacity_max_entity": cfg.huawei_solar_batteries_rated_capacity,
            "huawei_solar_batteries_state_of_capacity_entity": cfg.huawei_solar_batteries_state_of_capacity,
            "huawei_solar_batteries_tou_charging_and_discharging_periods_entity": cfg.huawei_solar_batteries_tou_charging_and_discharging_periods,
            "huawei_solar_batteries_working_mode_entity": cfg.huawei_solar_batteries_working_mode,
            "huawei_solar_device_id_batteries_id": cfg.huawei_solar_device_id_batteries,
            "huawei_solar_device_id_batteries_2_id": cfg.huawei_solar_device_id_batteries_2,
            "huawei_solar_device_id_inverter_1_id": cfg.huawei_solar_device_id_inverter_1,
            "huawei_solar_device_id_inverter_2_id": cfg.huawei_solar_device_id_inverter_2,
            "huawei_solar_inverter_active_power_control_state_entity": cfg.huawei_solar_inverter_active_power_control,
            "next_update": data.next_update,
            "read_only": cfg.read_only,
            "solar_production_power_entity": cfg.solar_production_power,
            "solcast_pv_forecast_forecast_today_entity": cfg.solcast_pv_forecast_forecast_today,
            "solcast_pv_forecast_forecast_tomorrow_entity": cfg.solcast_pv_forecast_forecast_tomorrow,
            "unique_id": unique_id,
            "update_interval": cfg.update_interval,
            "recommendation_interval_minutes": cfg.recommendation_interval_minutes,
            "recommendation_interval_length": cfg.recommendation_interval_length,
        }

    attributes = {
        "batteries_wait_mode_behavior": cfg.batteries_wait_mode_behavior,
        "wait_mode_self_consumption": _wait_mode_self_consumption(data, cfg, live),
        "batteries_current_capacity": live.battery_current_capacity_kwh,
        "batteries_usable_capacity": live.battery_usable_capacity_kwh,
        "batteries_recommended_min_price_threshold": calculate_recommended_threshold(
            purchase_price=cfg.batteries_purchase_price,
            expected_cycles=cfg.batteries_expected_cycles,
            usable_capacity=live.battery_usable_capacity_kwh,
            capacity_loss_pct=cfg.batteries_capacity_loss_pct,
        ),
        "batteries_capacity_loss_pct": cfg.batteries_capacity_loss_pct,
        "export_electricity_price_state": live.export_electricity_price,
        "import_electricity_price_state": live.import_electricity_price,
        "export_electricity_min_price": cfg.export_electricity_min_price,
        "electricity_price_update_interval": cfg.electricity_price_update_interval,
        "ev_charger_power_state": live.ev.power_w,
        "ev_charger_status_state": live.ev.is_charging,
        "ev_soc_state": live.ev.soc_pct,
        "ev_soc_target_state": live.ev.soc_target_pct,
        "ev_connected_state": live.ev.is_connected,
        "ev_allow_charge_past_target_soc": cfg.ev.allow_charge_past_target_soc,
        "ev_past_target_confidence_factor": cfg.ev.past_target_confidence_factor,
        "ev_charger_max_discharge_power_state": live.ev.max_discharge_power_w,
        "ev_charger_force_max_discharge_power": live.ev.force_max_discharge_power,
        # EV planned-load configuration — gates whether the planner/MILP
        # schedules EV charging at all. Exposed so missing charging can be
        # diagnosed directly from the working-mode sensor attributes.
        "ev_planned_load_enabled": cfg.ev_planned_load_enabled,
        "ev_planned_load_smart_charging_enabled": live.ev_planned_load_smart_charging_enabled,
        "ev_planned_load_battery_capacity_kwh": cfg.ev_planned_load_battery_capacity_kwh,
        "ev_planned_load_charger_power_kw": cfg.ev_planned_load_charger_power_kw,
        "ev_planned_load_charger_efficiency_pct": cfg.ev_planned_load_charger_efficiency_pct,
        "ev_planned_load_charger_min_power_w": cfg.ev_planned_load_charger_min_power_w,
        "ev_planned_load_charger_phase_topology": cfg.ev_planned_load_charger_phase_topology,
        "ev_planned_load_deadline": (
            live.ev_planned_load_deadline.isoformat()
            if live.ev_planned_load_deadline
            else None
        ),
        # Effective SoC = reported SoC + bounded delivered-energy credit.
        # The planner uses this value, not the raw reported SoC above —
        # when they diverge the plan may show fully_charged/waiting even
        # though the car reports below target.
        "ev_effective_soc_state": live.ev.effective_soc_pct,
        "ev_delivered_energy_credit_kwh": round(live.ev.delivered_energy_credit_kwh, 3),
        "ev_force_charge_now": bool(
            get_config_value(config_entry, "hsem_ev_force_charge_now")
        ),
        "ev_second_enabled": cfg.ev_second_enabled,
        "ev_second_planned_load_enabled": cfg.ev_second_planned_load_enabled,
        "ev_second_planned_load_battery_capacity_kwh": cfg.ev_second_planned_load_battery_capacity_kwh,
        "ev_second_planned_load_charger_power_kw": cfg.ev_second_planned_load_charger_power_kw,
        "ev_second_planned_load_smart_charging_enabled": live.ev_second_planned_load_smart_charging_enabled,
        "ev_second_planned_load_deadline": (
            live.ev_second_planned_load_deadline.isoformat()
            if live.ev_second_planned_load_deadline
            else None
        ),
        "ev_second_effective_soc_state": live.ev_second.effective_soc_pct,
        "ev_second_delivered_energy_credit_kwh": round(
            live.ev_second.delivered_energy_credit_kwh, 3
        ),
        "ev_second_force_charge_now": bool(
            get_config_value(config_entry, "hsem_ev_second_force_charge_now")
        ),
        "ev_second_planned_load_charger_efficiency_pct": cfg.ev_second_planned_load_charger_efficiency_pct,
        "ev_second_planned_load_charger_min_power_w": cfg.ev_second_planned_load_charger_min_power_w,
        "ev_second_planned_load_charger_phase_topology": cfg.ev_second_planned_load_charger_phase_topology,
        "ev_second_charger_power_state": live.ev_second.power_w,
        "ev_second_charger_status_state": live.ev_second.is_charging,
        "ev_second_soc_state": live.ev_second.soc_pct,
        "ev_second_soc_target_state": live.ev_second.soc_target_pct,
        "ev_second_connected_state": live.ev_second.is_connected,
        "ev_second_allow_charge_past_target_soc": cfg.ev_second.allow_charge_past_target_soc,
        "ev_second_past_target_confidence_factor": cfg.ev_second.past_target_confidence_factor,
        "ev_second_charger_max_discharge_power_state": live.ev_second.max_discharge_power_w,
        "ev_second_charger_force_max_discharge_power": live.ev_second.force_max_discharge_power,
        "force_working_mode_state": live.force_working_mode_state,
        "hourly_recommendation": data.hourly_recommendation,
        "hourly_recommendations": data.hourly_recommendations,
        "house_consumption_energy_weight_14d": cfg.house_consumption_energy_weight_14d,
        "house_consumption_energy_weight_1d": cfg.house_consumption_energy_weight_1d,
        "house_consumption_energy_weight_3d": cfg.house_consumption_energy_weight_3d,
        "house_consumption_energy_weight_7d": cfg.house_consumption_energy_weight_7d,
        "house_consumption_power_state": live.house_consumption_power_w,
        "house_power_includes_ev_charger_power": cfg.house_power_includes_ev_charger_power,
        "huawei_solar_batteries_charging_cutoff_capacity_state": live.huawei_batteries_charging_cutoff_capacity_pct,
        "huawei_solar_batteries_grid_charge_cutoff_soc_state": live.huawei_batteries_grid_charge_cutoff_soc_pct,
        "huawei_solar_batteries_maximum_charging_power_state": live.huawei_batteries_max_charge_power_w,
        "huawei_solar_batteries_maximum_discharging_power_state": live.huawei_batteries_max_discharge_power_w,
        "huawei_solar_batteries_rated_capacity_max_state": live.huawei_batteries_rated_capacity_wh,
        "huawei_solar_batteries_rated_capacity_min_state": live.battery_rated_capacity_min_kwh,
        "huawei_solar_batteries_state_of_capacity_state": live.huawei_batteries_soc_pct,
        "huawei_solar_batteries_tou_charging_and_discharging_periods_periods": live.tou_periods.periods,
        "huawei_solar_batteries_tou_charging_and_discharging_periods_state": live.tou_periods.raw_state,
        "huawei_solar_batteries_working_mode_state": live.huawei_batteries_working_mode,
        "huawei_solar_inverter_active_power_control_state_state": live.huawei_inverter_active_power_control,
        "huawei_solar_batteries_excess_pv_energy_use_in_tou_state": live.huawei_batteries_excess_pv_use_in_tou,
        "solcast_pv_forecast_forecast_likelihood": cfg.solcast_pv_forecast_forecast_likelihood,
        "last_updated": data.last_updated,
        "net_consumption_with_ev": live.net_consumption_with_ev_w,
        "net_consumption": live.net_consumption_w,
        "solar_production_power_state": live.solar_production_power_w,
        "months_winter": cfg.months_winter,
        "months_summer": cfg.months_summer,
        "batteries_enable_excess_export": cfg.batteries_enable_excess_export,
        "batteries_excess_export_discharge_buffer": cfg.batteries_excess_export_discharge_buffer,
        "main_fuse_amps": cfg.main_fuse_amps,
        "phase_aware_charging_enabled": cfg.phase_aware_charging_enabled,
        "grid_phase_readings": live.grid_phase_readings,
        "grid_phase_voltage_v": live.grid_phase_voltage_v,
        "huawei_batteries_grid_charge_max_power_w": live.huawei_batteries_grid_charge_max_power_w,
        "primary_grid_charge_owned": primary_grid_charge_owned,
        # House-battery target SoC diagnostics for the next occurrence
        # (issue #1109); None when the target is disabled.
        "battery_target": data.battery_target,
    }

    return {**attributes, **extended}
