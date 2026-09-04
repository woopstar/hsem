"""Pure-Python dataclass representing the full configuration of HSEMWorkingModeSensor.

This module is the single source of truth for all config-entry values that the
sensor reads during each update cycle.  It carries **no** Home Assistant imports
and can therefore be constructed and inspected in plain unit tests without a
running HA instance.

The :func:`build_sensor_config` factory function (in
``custom_sensors/state_collector.py``) is responsible for reading a
``ConfigEntry`` and returning a populated :class:`SensorConfig`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import cast

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES


@dataclass
class EVChargerConfig:
    """Configuration for a single EV charger.

    Holds the entity IDs for the charger's status, power, SoC, and
    connection sensors, along with behavioural flags such as whether to
    allow charging past the target SoC and force maximum discharge power.
    """

    status_entity: str | None = None
    power_entity: str | None = None
    soc_entity: str | None = None
    connected_entity: str | None = None
    allow_charge_past_target_soc: bool = False
    #: Confidence multiplier (0.0-1.0) applied to the avoided-future-import
    #: valuation of charge-past-target EV charging in the MILP. See
    #: ``ev_future_charge_value_per_kwh`` in ``planner/candidate_selector.py``.
    past_target_confidence_factor: float = 0.9
    force_max_discharge_power: bool = False
    max_discharge_power: int = 0


@dataclass
class BatteryScheduleConfig:
    """Configuration for one charge/discharge battery schedule window.

    Defines whether the schedule window is enabled and its start/end times.
    Used as a nested config block within :class:`SensorConfig` for up to
    three independent battery schedule windows.
    """

    enabled: bool = False
    start: time | None = None
    end: time | None = None


@dataclass
class SensorConfig:
    """Complete set of configuration values for :class:`HSEMWorkingModeSensor`.

    All fields are plain Python types; no Home Assistant objects are stored here.
    Fields mirror the config-entry option keys defined in ``const.py`` and
    populated by ``_update_settings()`` in the sensor.

    Attributes:
        read_only: When True the sensor skips all hardware writes.
        verbose_logging: Enable extra debug log output.
        extended_attributes: Expose extra diagnostic attributes on the entity.
        update_interval: Polling interval in minutes.
        recommendation_interval_minutes: Slot width in minutes (15 or 60).
        recommendation_interval_length: Planning horizon in hours.
        electricity_price_update_interval: Price sensor update cadence in minutes (15, 30, or 60).

        huawei_solar_device_id_inverter_1: Device ID for inverter 1.
        huawei_solar_device_id_inverter_2: Device ID for inverter 2 (optional).
        huawei_solar_device_id_batteries: Device ID for the primary battery pack.
        huawei_solar_device_id_batteries_2: Device ID for the secondary battery pack (optional).
        huawei_solar_batteries_working_mode: Entity ID for working mode select.
        huawei_solar_batteries_end_of_discharge_soc: Entity ID for EoD SoC number.
        huawei_solar_batteries_state_of_capacity: Entity ID for SoC sensor.
        huawei_solar_batteries_charging_cutoff_capacity: Entity ID for charging cutoff SoC (max SoC).
        huawei_solar_batteries_grid_charge_cutoff_soc: Entity ID for grid charge cutoff SoC.
        huawei_solar_batteries_maximum_charging_power: Entity ID for max charge power.
        huawei_solar_batteries_maximum_discharging_power: Entity ID for max discharge power.
        huawei_solar_batteries_grid_charge_maximum_power: Entity ID for the grid-charge
            maximum-power number, written by the live phase-aware charging limiter.
        huawei_solar_batteries_charge_discharge_power: Entity ID for the signed
            instantaneous battery charge/discharge power sensor.
        huawei_solar_power_meter_phase_a_active_power: Entity ID for phase A live power.
        huawei_solar_power_meter_phase_b_active_power: Entity ID for phase B live power.
        huawei_solar_power_meter_phase_c_active_power: Entity ID for phase C live power.
        huawei_solar_batteries_tou_charging_and_discharging_periods: Entity ID for TOU periods.
        huawei_solar_batteries_excess_pv_energy_use_in_tou: Entity ID for excess PV use select.
        huawei_solar_inverter_active_power_control: Entity ID for export power control.
        huawei_solar_batteries_rated_capacity: Entity ID for rated battery capacity sensor.

        house_consumption_power: Entity ID for house power meter.
        solar_production_power: Entity ID for solar production meter.
        house_power_includes_ev_charger_power: True if EV draw is already in house meter.

        solcast_pv_forecast_forecast_today: Entity ID for today's Solcast forecast.
        solcast_pv_forecast_forecast_tomorrow: Entity ID for tomorrow's Solcast forecast.
        solcast_pv_forecast_forecast_likelihood: Attribute key for Solcast estimate field.

        import_electricity_price_sensor: Entity ID for the import price sensor.
        export_electricity_price_sensor: Entity ID for the export price sensor.
        import_electricity_price_forecast_sensor: Optional entity ID for a separate import forecast sensor (e.g. Amber Electric).
        export_electricity_price_forecast_sensor: Optional entity ID for a separate export forecast sensor.
        export_electricity_min_price: Minimum export price to allow grid export.

        ev: First EV charger configuration.
        ev_second_enabled: Whether the second EV charger is active.
        ev_second: Second EV charger configuration.

        batteries_charge_efficiency: Charge efficiency as a percentage (0-100).
            Energy stored in the battery = input_energy × (charge_efficiency / 100).
            Defaults to 97 % (3 % charge-side loss).
        batteries_discharge_efficiency: Discharge efficiency as a percentage (0-100).
            Energy delivered to the house = battery_energy × (discharge_efficiency / 100).
            Defaults to 97 % (3 % discharge-side loss).
        batteries_purchase_price: Battery pack purchase price for depreciation calc.
        batteries_expected_cycles: Expected total cycle life of the battery.
        batteries_cycle_cost: User-configured extra per-kWh cycle cost.
            Added to the auto-derived depreciation threshold.  0.0 = disabled.

        batteries_schedule_1: First discharge-window schedule config.
        batteries_schedule_2: Second discharge-window schedule config.
        batteries_schedule_3: Third discharge-window schedule config.

        batteries_enable_excess_export: Enable opportunistic forced-discharge export.
        batteries_excess_export_discharge_buffer: Safety buffer percentage to keep.
        batteries_forecast_reserve_pct: Extra SoC percentage points above the
            Huawei end-of-discharge limit that intentional battery export must
            leave available for unexpected local demand.
        batteries_export_min_price: Per-slot hard floor for intentional
            battery-to-grid export (issue #752).  ``0.0`` disables the floor.
        batteries_wait_mode_behavior: How ``batteries_wait_mode`` is interpreted.
            ``strict`` keeps the battery idle; ``self_consumption_with_reserve``
            allows normal household self-consumption while protecting the
            planner's required battery reserve.

        phase_aware_charging_enabled: Enable the live per-phase Huawei
            grid-charge safety limiter (issue #831).

        months_winter: List of month integers (1-12) treated as winter.
        months_summer: List of month integers (1-12) treated as summer.

        house_consumption_energy_weight_1d: Weight (%) for 1-day consumption average.
        house_consumption_energy_weight_3d: Weight (%) for 3-day consumption average.
        house_consumption_energy_weight_7d: Weight (%) for 7-day consumption average.
        house_consumption_energy_weight_14d: Weight (%) for 14-day consumption average.
    """

    # General
    read_only: bool = False
    verbose_logging: bool = True
    extended_attributes: bool = False
    update_interval: int = 1
    recommendation_interval_minutes: int = 15
    recommendation_interval_length: int = 48
    electricity_price_update_interval: int = 15

    # Huawei Solar device IDs
    huawei_solar_device_id_inverter_1: str | None = None
    huawei_solar_device_id_inverter_2: str | None = None
    huawei_solar_device_id_batteries: str | None = None
    huawei_solar_device_id_batteries_2: str | None = None

    # Huawei Solar entity IDs
    huawei_solar_batteries_working_mode: str | None = None
    huawei_solar_batteries_end_of_discharge_soc: str | None = None
    huawei_solar_batteries_state_of_capacity: str | None = None
    huawei_solar_batteries_charging_cutoff_capacity: str | None = None
    huawei_solar_batteries_grid_charge_cutoff_soc: str | None = None
    huawei_solar_batteries_maximum_charging_power: str | None = None
    huawei_solar_batteries_maximum_discharging_power: str | None = None
    #: Entity ID for the Huawei grid-charge maximum-power number (issue #831).
    #: Written by the live phase-aware charging limiter to cap grid-funded
    #: charge power below the fuse limit; ``None`` disables the write path.
    huawei_solar_batteries_grid_charge_maximum_power: str | None = None
    #: Entity ID for the signed instantaneous battery charge/discharge power
    #: sensor (issue #831). Positive = charging, negative = discharging.
    #: Required by the live phase-aware charging limiter to remove Huawei's
    #: own contribution from the live phase snapshot before computing
    #: headroom; ``None`` makes the limiter fail closed.
    huawei_solar_batteries_charge_discharge_power: str | None = None
    huawei_solar_batteries_tou_charging_and_discharging_periods: str | None = None
    huawei_solar_batteries_excess_pv_energy_use_in_tou: str | None = None
    huawei_solar_batteries_forcible_charge: str | None = None
    huawei_solar_inverter_active_power_control: str | None = None
    huawei_solar_batteries_rated_capacity: str | None = None
    #: Entity IDs for the three Huawei power-meter phase active-power sensors
    #: (issue #831).  All three must be configured for the live phase-aware
    #: charging limiter to activate.
    huawei_solar_power_meter_phase_a_active_power: str | None = None
    huawei_solar_power_meter_phase_b_active_power: str | None = None
    huawei_solar_power_meter_phase_c_active_power: str | None = None

    # Power meters
    house_consumption_power: str | None = None
    solar_production_power: str | None = None
    house_power_includes_ev_charger_power: bool = False
    #: Main fuse/breaker rating in amps (0 = disabled).  The MILP optimizer
    #: uses this as a soft constraint on total grid import power.
    main_fuse_amps: int = 0
    #: Electrical phase count (1 or 3).  Single-phase installations MUST
    #: set this to 1 — using 3 on a single-phase install makes the fuse
    #: protection constraint silenty 3× too permissive.
    main_fuse_phases: int = 3
    #: Maximum grid export power in kW (0 = disabled).  DNO/inverter export
    #: cap for export-limited connections (issue #726).  The MILP optimizer
    #: uses this as a hard bound on per-slot grid export energy.
    max_grid_export_power_kw: float = 0.0
    #: Enable the live per-phase Huawei grid-charge safety limiter (issue
    #: #831).  Requires ``main_fuse_phases == 3`` and both the grid-charge
    #: maximum-power entity and all three phase power-meter entities to be
    #: configured.  Disabled by default — fully backward compatible.
    phase_aware_charging_enabled: bool = False

    # Solcast
    solcast_pv_forecast_forecast_today: str | None = None
    solcast_pv_forecast_forecast_tomorrow: str | None = None
    solcast_pv_forecast_forecast_likelihood: str = "pv_estimate"

    # Electricity prices (generic — supports Energi Data Service, Nordpool, Amber Electric, …)
    import_electricity_price_sensor: str | None = None
    export_electricity_price_sensor: str | None = None
    import_electricity_price_forecast_sensor: str | None = None
    export_electricity_price_forecast_sensor: str | None = None
    export_electricity_min_price: float = 0.0

    # EV chargers
    ev: EVChargerConfig = field(default_factory=EVChargerConfig)
    ev_second_enabled: bool = False
    ev_second: EVChargerConfig = field(default_factory=EVChargerConfig)
    #: Auto-Full on negative electricity price (issue #609)
    ev_auto_full_negative_price: bool = False

    # Battery economics
    batteries_charge_efficiency: float = 98.0
    batteries_discharge_efficiency: float = 98.0
    batteries_purchase_price: float = 0.0
    batteries_expected_cycles: int = 6000
    #: User-configured per-kWh cycle cost. When > 0 it is added directly to
    #: the min-price-difference guard so cycling only happens when the price
    #: spread covers both losses AND wear.  0.0 means no extra guard.
    batteries_cycle_cost: float = 0.0

    #: Expected battery capacity loss at end-of-life as a percentage (0-100).
    #: LiFePO4 EOL is typically 20 % (80 % retained).  Default 30 % includes
    #: margin for calendar ageing.
    batteries_capacity_loss_pct: float = field(
        default_factory=lambda: cast(
            float, DEFAULT_CONFIG_VALUES["hsem_batteries_capacity_loss_pct"]
        )
    )

    # Battery discharge schedules
    batteries_schedule_1: BatteryScheduleConfig = field(
        default_factory=BatteryScheduleConfig
    )
    batteries_schedule_2: BatteryScheduleConfig = field(
        default_factory=BatteryScheduleConfig
    )
    batteries_schedule_3: BatteryScheduleConfig = field(
        default_factory=BatteryScheduleConfig
    )

    # Excess export
    batteries_enable_excess_export: bool = False
    batteries_excess_export_discharge_buffer: float = 10.0
    #: Extra SoC percentage points above Huawei's end-of-discharge limit that
    #: intentional battery export must leave after each export slot. Ordinary
    #: self-consumption may use this energy when actual demand exceeds forecast.
    batteries_forecast_reserve_pct: float = 0.0
    #: Per-slot hard floor for intentional battery-to-grid export (issue #752).
    #: When ``> 0`` and a slot's export price is strictly below this floor the
    #: MILP caps the battery discharge variable so the battery can only cover
    #: house load (no grid export).  ``0.0`` = disabled (default) — fully
    #: backward compatible.
    batteries_export_min_price: float = 0.0

    # Wait mode behaviour
    batteries_wait_mode_behavior: str = "strict"

    # EV planned load integration — primary EV (optional, disabled by default)
    ev_planned_load_enabled: bool = False
    ev_planned_load_battery_capacity_kwh: float = 0.0
    ev_planned_load_charger_power_kw: float = 0.0
    ev_planned_load_charger_efficiency_pct: float = 100.0
    ev_planned_load_charger_min_power_w: float = 1380.0
    #: Electrical topology of the primary charger (see ``utils/phase_power``).
    ev_planned_load_charger_phase_topology: str = "single_phase"
    #: Percentage of the deadline shortfall budgeted as extra safety margin
    #: above the target SoC, so execution-layer friction doesn't turn an
    #: exact plan into a missed deadline (issue #845).
    ev_planned_load_deadline_safety_margin_pct: float = 0.0
    #: Minimum whole-amp *reduction* the plan must ask for before the live
    #: charger ceiling is lowered.  Raising the ceiling is never held.
    #: Damps integer-lattice churn that costs nothing to ignore.
    #: 0 disables the deadband.
    ev_planned_load_command_deadband_a: float = 3.0
    #: Tail of a slot (minutes) during which a zero command is suppressed
    #: while unmet EV need remains, so a few seconds of leftover slot
    #: cannot stop and restart the charging session.  0 disables it.
    ev_planned_load_stub_floor_minutes: float = 2.0
    # EV planned load integration — second EV (optional, disabled by default)
    ev_second_planned_load_enabled: bool = False
    ev_second_planned_load_battery_capacity_kwh: float = 0.0
    ev_second_planned_load_charger_power_kw: float = 0.0
    ev_second_planned_load_charger_efficiency_pct: float = 100.0
    ev_second_planned_load_charger_min_power_w: float = 1380.0
    #: Electrical topology of the second charger (see ``utils/phase_power``).
    ev_second_planned_load_charger_phase_topology: str = "single_phase"
    #: Same as ev_planned_load_deadline_safety_margin_pct, for the second EV.
    ev_second_planned_load_deadline_safety_margin_pct: float = 0.0
    #: Same as ev_planned_load_command_deadband_a, for the second EV.
    ev_second_planned_load_command_deadband_a: float = 3.0
    #: Same as ev_planned_load_stub_floor_minutes, for the second EV.
    ev_second_planned_load_stub_floor_minutes: float = 2.0

    # Seasonal configuration
    months_winter: list[int] = field(default_factory=list)
    months_summer: list[int] = field(default_factory=list)

    # Daily plan-vs-actual tracking — optional cumulative energy meter entities.
    # When not configured, the sensor falls back to Riemann sums from power sensors.
    grid_import_energy_entity: str | None = None
    grid_export_energy_entity: str | None = None
    pv_energy_entity: str | None = None

    # ML consumption prediction — toggle and settings
    ml_consumption_enabled: bool = False
    ml_consumption_energy_entity: str | None = None
    ml_consumption_history_days: int = 14
    ml_consumption_net_consumption: bool = False
    ml_consumption_sequential: bool = False
    ml_consumption_temperature_entity: str | None = None
    #: Optional weather entity supplying forecast temperatures for FUTURE
    #: inference slots.  Requires ml_consumption_temperature_entity to also
    #: be configured (and to have trained a temperature-aware model) —
    #: otherwise the predictor has no temperature coefficient to feed.
    ml_consumption_weather_forecast_entity: str | None = None

    # Planner hysteresis — keep the active plan unless a new plan is
    # materially better (anti-flapping, issue #372).
    planner_hysteresis_enabled: bool = True
    planner_hysteresis_absolute: float = 0.0
    planner_hysteresis_percentage: float = 5.0
    # Window-level hysteresis — minimum hold time (minutes) before allowing
    # any recommendation change.  0 disables the feature.
    planner_window_hysteresis_minutes: int = 10

    # Embedded OCPP 1.6 server for EV charger control (issue #603).
    ocpp_enabled: bool = False
    ocpp_port: int = 9000
    ocpp_cpid: str = ""
    ocpp_start_window_s: int = 60
    ocpp_stop_window_s: int = 180
    # Second OCPP server for the optional second EV (separate port).
    ocpp_second_enabled: bool = False
    ocpp_second_port: int = 9001
    ocpp_second_cpid: str = ""

    # Consumption weights
    house_consumption_energy_weight_1d: int = 50
    house_consumption_energy_weight_3d: int = 20
    house_consumption_energy_weight_7d: int = 15
    house_consumption_energy_weight_14d: int = 10

    def schedule_configs(self) -> list[BatteryScheduleConfig]:
        """Return all three schedule configs as a list."""
        return [
            self.batteries_schedule_1,
            self.batteries_schedule_2,
            self.batteries_schedule_3,
        ]

    def __repr__(self) -> str:
        return (
            f"SensorConfig(read_only={self.read_only}, "
            f"interval_minutes={self.recommendation_interval_minutes}, "
            f"interval_length_hours={self.recommendation_interval_length}, "
            f"weights=[{self.house_consumption_energy_weight_1d}/"
            f"{self.house_consumption_energy_weight_3d}/"
            f"{self.house_consumption_energy_weight_7d}/"
            f"{self.house_consumption_energy_weight_14d}])"
        )
