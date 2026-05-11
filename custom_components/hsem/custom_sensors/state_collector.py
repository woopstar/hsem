"""State collector for HSEMWorkingModeSensor.

Single responsibility: read all relevant HA entity states and config-entry
options at the start of an update cycle, and return two typed snapshots:

- :class:`~custom_components.hsem.models.sensor_config.SensorConfig` — static
  configuration extracted from ``config_entry.options``.
- :class:`~custom_components.hsem.models.live_state.LiveState` — dynamic entity
  states read from ``hass.states`` at call time.

No side-effects are performed here.  Hardware writes, forecast computation, and
recommendation decisions all happen in other modules.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.hsem.models.battery_schedule import BatterySchedule
from custom_components.hsem.models.live_state import (
    EVLiveState,
    LiveState,
    TouPeriodsState,
)
from custom_components.hsem.models.sensor_config import (
    BatteryScheduleConfig,
    EVChargerConfig,
    SensorConfig,
)
from custom_components.hsem.utils.logger import async_logger
from custom_components.hsem.utils.misc import (
    async_resolve_entity_id_from_unique_id,
    convert_months_to_int,
    convert_to_boolean,
    convert_to_float,
    convert_to_int,
    convert_to_time,
    get_config_value,
    ha_get_entity_state_and_convert,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config-entry → SensorConfig
# ---------------------------------------------------------------------------


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

    cfg.read_only = get_config_value(config_entry, "hsem_read_only")
    cfg.verbose_logging = get_config_value(config_entry, "hsem_verbose_logging")
    cfg.extended_attributes = get_config_value(config_entry, "hsem_extended_attributes")
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

    # --- Seasonal months ---
    months_winter = get_config_value(config_entry, "hsem_months_winter")
    months_summer = get_config_value(config_entry, "hsem_months_summer")
    cfg.months_winter = (
        convert_months_to_int(months_winter) if isinstance(months_winter, list) else []
    )
    cfg.months_summer = (
        convert_months_to_int(months_summer) if isinstance(months_summer, list) else []
    )

    # --- Huawei Solar device IDs ---
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

    # --- Huawei Solar entity IDs ---
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
        config_entry,
        "hsem_huawei_solar_batteries_excess_pv_energy_use_in_tou",
    )
    cfg.huawei_solar_inverter_active_power_control = get_config_value(
        config_entry, "hsem_huawei_solar_inverter_active_power_control"
    )
    cfg.huawei_solar_batteries_rated_capacity = get_config_value(
        config_entry, "hsem_huawei_solar_batteries_rated_capacity"
    )

    # --- Power meters ---
    cfg.house_consumption_power = get_config_value(
        config_entry, "hsem_house_consumption_power"
    )
    cfg.solar_production_power = get_config_value(
        config_entry, "hsem_solar_production_power"
    )
    cfg.house_power_includes_ev_charger_power = convert_to_boolean(
        get_config_value(config_entry, "hsem_house_power_includes_ev_charger_power")
    )

    # --- Solcast ---
    cfg.solcast_pv_forecast_forecast_today = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_today"
    )
    cfg.solcast_pv_forecast_forecast_tomorrow = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_tomorrow"
    )
    cfg.solcast_pv_forecast_forecast_likelihood = get_config_value(
        config_entry, "hsem_solcast_pv_forecast_forecast_likelihood"
    )

    # --- Energi Data Service ---
    cfg.energi_data_service_import = get_config_value(
        config_entry, "hsem_energi_data_service_import"
    )
    cfg.energi_data_service_export = get_config_value(
        config_entry, "hsem_energi_data_service_export"
    )
    cfg.energi_data_service_export_min_price = convert_to_float(
        get_config_value(config_entry, "hsem_energi_data_service_export_min_price")
    )

    # --- First EV charger ---
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

    # --- Second EV charger ---
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

    # --- Battery economics ---
    cfg.batteries_conversion_loss = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_conversion_loss")
    )
    cfg.batteries_purchase_price = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_purchase_price")
    )
    cfg.batteries_expected_cycles = convert_to_int(
        get_config_value(config_entry, "hsem_batteries_expected_cycles")
    )

    # --- Battery schedules ---
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

    # --- Excess export ---
    cfg.batteries_enable_excess_export = get_config_value(
        config_entry, "hsem_batteries_enable_excess_export"
    )
    cfg.batteries_excess_export_discharge_buffer = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_excess_export_discharge_buffer")
    )
    cfg.batteries_excess_export_price_threshold = convert_to_float(
        get_config_value(config_entry, "hsem_batteries_excess_export_price_threshold")
    )

    # --- Consumption weights ---
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
# HA entity states → LiveState
# ---------------------------------------------------------------------------


async def async_collect_live_state(
    sensor,
    cfg: SensorConfig,
    force_working_mode_cache: str | None,
    tracked_entities: set[str],
) -> tuple[LiveState, str | None]:
    """Read all HA entity states and return a populated :class:`LiveState`.

    This is the **only** function in the module that touches ``sensor.hass``.
    All other functions accept plain dataclasses.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance (used for ``hass`` access,
            ``entity_id``, and :func:`async_logger`).
        cfg: Current sensor configuration (determines which entities to read).
        force_working_mode_cache: Previously resolved entity_id for the force
            working mode select, or ``None`` to trigger resolution.
        tracked_entities: Mutable set of entity_ids already registered for
            state-change tracking.  Updated in-place when new entities are added.

    Returns:
        A ``(LiveState, updated_force_working_mode_entity_id)`` tuple.  The second
        element is the (possibly newly resolved) force working mode entity_id so
        the caller can persist it between cycles.
    """
    state = LiveState()

    # --- Resolve force working mode entity once ---
    fwm_entity = force_working_mode_cache
    if fwm_entity is None:
        fwm_entity = await async_resolve_entity_id_from_unique_id(
            sensor, "hsem_force_working_mode", "select"
        )
    state.force_working_mode = fwm_entity

    def _read(
        entity_id: str | None, conv_type=None, decimals: int = 3, label: str = ""
    ):
        """Read one entity state, recording it as missing on any failure."""
        if not entity_id:
            state.add_missing_entity(f"Missing entity: {label or entity_id}")
            return None
        try:
            return ha_get_entity_state_and_convert(
                sensor, entity_id, conv_type, decimals
            )
        except Exception as exc:
            state.add_missing_entity(
                f"Error reading {label or entity_id}: {type(exc).__name__}: {exc}"
            )
            return None

    # Force working mode
    raw_fwm = _read(fwm_entity, "string", label="hsem_force_working_mode")
    state.force_working_mode_state = raw_fwm if raw_fwm is not None else "auto"

    # --- First EV charger ---
    ev = EVLiveState()
    ev.force_max_discharge_power = cfg.ev.force_max_discharge_power
    ev.max_discharge_power_w = cfg.ev.max_discharge_power
    if cfg.ev.status_entity:
        ev.is_charging = convert_to_boolean(
            _read(cfg.ev.status_entity, "boolean", label="ev_charger_status")
        )
    if cfg.ev.power_entity:
        ev.power_w = convert_to_float(
            _read(cfg.ev.power_entity, "float", label="ev_charger_power")
        )
    if cfg.ev.soc_entity:
        ev.soc_pct = convert_to_float(_read(cfg.ev.soc_entity, "float", label="ev_soc"))
    if cfg.ev.soc_target_entity:
        ev.soc_target_pct = convert_to_float(
            _read(cfg.ev.soc_target_entity, "float", label="ev_soc_target")
        )
    if cfg.ev.connected_entity:
        ev.is_connected = convert_to_boolean(
            _read(cfg.ev.connected_entity, "boolean", label="ev_connected")
        )
    state.ev = ev

    # --- Second EV charger ---
    ev2 = EVLiveState()
    ev2.force_max_discharge_power = cfg.ev_second.force_max_discharge_power
    ev2.max_discharge_power_w = cfg.ev_second.max_discharge_power
    if cfg.ev_second.status_entity:
        ev2.is_charging = convert_to_boolean(
            _read(
                cfg.ev_second.status_entity,
                "boolean",
                label="ev_second_charger_status",
            )
        )
    if cfg.ev_second.power_entity:
        ev2.power_w = convert_to_float(
            _read(cfg.ev_second.power_entity, "float", label="ev_second_charger_power")
        )
    if cfg.ev_second.soc_entity:
        ev2.soc_pct = convert_to_float(
            _read(cfg.ev_second.soc_entity, "float", label="ev_second_soc")
        )
    if cfg.ev_second.soc_target_entity:
        ev2.soc_target_pct = convert_to_float(
            _read(
                cfg.ev_second.soc_target_entity,
                "float",
                label="ev_second_soc_target",
            )
        )
    if cfg.ev_second.connected_entity:
        ev2.is_connected = convert_to_boolean(
            _read(
                cfg.ev_second.connected_entity,
                "boolean",
                label="ev_second_connected",
            )
        )
    state.ev_second = ev2

    # --- Power meters ---
    state.house_consumption_power_w = (
        convert_to_float(
            _read(cfg.house_consumption_power, "float", label="house_consumption_power")
        )
        or 0.0
    )
    state.solar_production_power_w = (
        convert_to_float(
            _read(cfg.solar_production_power, "float", label="solar_production_power")
        )
        or 0.0
    )

    # --- Huawei Solar battery entities ---
    state.huawei_batteries_excess_pv_use_in_tou = _read(
        cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou,
        "string",
        label="excess_pv_energy_use_in_tou",
    )
    state.huawei_batteries_working_mode = _read(
        cfg.huawei_solar_batteries_working_mode,
        "string",
        label="batteries_working_mode",
    )
    state.huawei_batteries_soc_pct = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_state_of_capacity,
            "float",
            label="state_of_capacity",
        )
    )
    eod_soc = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_end_of_discharge_soc,
            "float",
            label="end_of_discharge_soc",
        )
    )
    state.huawei_batteries_end_of_discharge_soc_pct = eod_soc if eod_soc else 5.0
    state.huawei_batteries_grid_charge_cutoff_soc_pct = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_grid_charge_cutoff_soc,
            "float",
            label="grid_charge_cutoff_soc",
        )
    )
    state.huawei_batteries_max_charge_power_w = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_maximum_charging_power,
            "float",
            label="max_charging_power",
        )
    )
    state.huawei_batteries_max_discharge_power_w = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_maximum_discharging_power,
            "float",
            label="max_discharging_power",
        )
    )
    state.huawei_batteries_rated_capacity_wh = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_rated_capacity,
            "float",
            label="batteries_rated_capacity_max",
        )
    )
    state.huawei_inverter_active_power_control = _read(
        cfg.huawei_solar_inverter_active_power_control,
        "string",
        label="inverter_active_power_control",
    )

    # --- EDS prices ---
    state.energi_data_service_import_price = (
        convert_to_float(
            _read(cfg.energi_data_service_import, "float", 3, label="eds_import")
        )
        or 0.0
    )
    state.energi_data_service_export_price = (
        convert_to_float(
            _read(cfg.energi_data_service_export, "float", 3, label="eds_export")
        )
        or 0.0
    )

    # --- TOU periods (special: need State object, not just string) ---
    tou = TouPeriodsState()
    if cfg.huawei_solar_batteries_tou_charging_and_discharging_periods:
        try:
            from homeassistant.core import State  # noqa: PLC0415

            entity_data = ha_get_entity_state_and_convert(
                sensor,
                cfg.huawei_solar_batteries_tou_charging_and_discharging_periods,
                None,
            )
            if isinstance(entity_data, State):
                tou.raw_state = entity_data.state
                tou.periods = [
                    entity_data.attributes[f"Period {i}"]
                    for i in range(1, 11)
                    if f"Period {i}" in entity_data.attributes
                ]
            else:
                state.add_missing_entity("TOU periods entity is not of type State")
        except Exception as exc:
            state.add_missing_entity(
                f"Error reading TOU periods: {type(exc).__name__}: {exc}"
            )
    else:
        state.add_missing_entity("Missing entity: TOU periods")
    state.tou_periods = tou

    # --- Derived battery capacities ---
    _compute_battery_capacities(state)

    # --- Net consumption ---
    _compute_net_consumption(state, cfg)

    # --- Register state-change listeners for reactive entities ---
    await _register_listeners(sensor, cfg, state, tracked_entities)

    return state, fwm_entity


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _optional_entity(value) -> str | None:
    """Return None if value is vol.UNDEFINED or falsy, else the string."""
    if value is vol.UNDEFINED or not value:
        return None
    return value


def _compute_battery_capacities(state: LiveState) -> None:
    """Fill ``battery_usable_capacity_kwh``, ``battery_current_capacity_kwh``,
    and ``battery_rated_capacity_min_kwh`` from the raw entity readings.

    This is the extracted logic from
    ``_async_calculate_remaining_battery_capacity`` in the sensor.
    """
    rated_wh = state.huawei_batteries_rated_capacity_wh
    soc_pct = state.huawei_batteries_soc_pct

    if not isinstance(rated_wh, (int, float)) or not isinstance(soc_pct, (int, float)):
        return

    rated_kwh = rated_wh / 1000.0
    eod_soc = state.huawei_batteries_end_of_discharge_soc_pct or 5.0
    reserve_kwh = rated_kwh * (eod_soc / 100.0)
    usable_kwh = max(rated_kwh - reserve_kwh, 0.0)
    current_kwh = (soc_pct / 100.0) * rated_kwh
    available_kwh = max(current_kwh - reserve_kwh, 0.0)

    state.battery_rated_capacity_min_kwh = round(reserve_kwh, 3)
    state.battery_usable_capacity_kwh = round(usable_kwh, 2)
    state.battery_current_capacity_kwh = round(available_kwh, 2)


def _compute_net_consumption(state: LiveState, cfg: SensorConfig) -> None:
    """Compute ``net_consumption_w`` and ``net_consumption_with_ev_w``.

    Extracted from ``_async_calculate_net_consumption`` in the sensor.
    """
    house_w = state.house_consumption_power_w
    solar_w = state.solar_production_power_w

    if not isinstance(house_w, (int, float)) or not isinstance(solar_w, (int, float)):
        state.net_consumption_w = 0.0
        return

    ev_w = (state.ev.power_w or 0.0) + (state.ev_second.power_w or 0.0)

    if cfg.house_power_includes_ev_charger_power:
        state.net_consumption_with_ev_w = round(house_w - solar_w, 3)
        state.net_consumption_w = round(house_w - solar_w - ev_w, 3)
    else:
        state.net_consumption_with_ev_w = round(house_w - solar_w + ev_w, 3)
        state.net_consumption_w = round(house_w - solar_w, 3)


async def _register_listeners(
    sensor,
    cfg: SensorConfig,
    state: LiveState,
    tracked_entities: set[str],
) -> None:
    """Register ``async_track_state_change_event`` for reactive entities.

    Only entities that have not been tracked before are registered (idempotent).
    """
    candidates = [
        cfg.ev.status_entity,
        cfg.ev.connected_entity,
        cfg.ev_second.status_entity,
        cfg.ev_second.connected_entity,
        state.force_working_mode,
    ]

    for entity_id in candidates:
        if entity_id and entity_id not in tracked_entities:
            await async_logger(
                sensor,
                f"Starting to track state changes for {entity_id}",
            )
            async_track_state_change_event(
                sensor.hass, [entity_id], sensor._async_handle_update
            )
            tracked_entities.add(entity_id)
