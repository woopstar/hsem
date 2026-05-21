"""State collector for HSEMWorkingModeSensor.

Single responsibility: read live HA entity states and return a typed
:class:`~custom_components.hsem.models.live_state.LiveState` snapshot.

Config-entry reading has moved to :mod:`config_reader`.
Both :func:`build_sensor_config` and :func:`build_battery_schedules` are
re-exported here so existing callers continue to work without changes.

This module also collects ALL HA states into an immutable
:class:`~custom_components.hsem.models.state_snapshot.StateSnapshot`
so that downstream population functions never need additional
``hass.states.get()`` lookups.
"""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event

from custom_components.hsem.custom_sensors.config_reader import (  # noqa: F401
    build_battery_schedules,
    build_sensor_config,
)
from custom_components.hsem.models.live_state import (
    EVLiveState,
    LiveState,
    TouPeriodsState,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.logger import async_logger
from custom_components.hsem.utils.misc import (
    async_resolve_entity_id_from_unique_id,
    convert_to_boolean,
    convert_to_float,
    ha_get_entity_state_and_convert,
)
from custom_components.hsem.utils.sensornames import (
    get_energy_average_sensor_entity_id,
    get_energy_average_sensor_unique_id,
    get_force_working_mode_selector_key,
)

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HA entity states → LiveState
# ---------------------------------------------------------------------------


async def async_collect_live_state(
    sensor,
    cfg: SensorConfig,
    force_working_mode_cache: str | None,
    tracked_entities: set[str],
) -> tuple[LiveState, str | None, list]:
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
        A ``(LiveState, updated_force_working_mode_entity_id, new_unsub_callbacks)``
        tuple.  The third element is a list of new unsubscribe callables for
        listeners registered during this call; the caller is responsible for
        storing and eventually cancelling them.
    """
    state = LiveState()

    # --- Resolve force working mode entity once ---
    fwm_entity = force_working_mode_cache
    if fwm_entity is None:
        fwm_entity = await async_resolve_entity_id_from_unique_id(
            sensor, get_force_working_mode_selector_key(), "select"
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
        except (HomeAssistantError, ValueError, TypeError, AttributeError) as exc:
            state.add_missing_entity(
                f"Error reading {label or entity_id} (entity_id={entity_id}): "
                f"{type(exc).__name__}: {exc}"
            )
            _LOGGER.warning(
                "Sensor read failed for entity_id=%s (label=%s): %s: %s",
                entity_id,
                label or entity_id,
                type(exc).__name__,
                repr(exc),
            )
            return None

    # Force working mode
    raw_fwm = _read(fwm_entity, "string", label=get_force_working_mode_selector_key())
    # Cast to str because _read() returns Any; str() is safe here since the
    # value comes from a HA select entity that always produces a string state.
    state.force_working_mode_state = str(raw_fwm) if raw_fwm is not None else "auto"

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
    # _read() returns float|int|bool|str|None; these calls use conv_type="string"
    # so the result is always str|None — cast explicitly for pyright.
    _raw_excess = _read(
        cfg.huawei_solar_batteries_excess_pv_energy_use_in_tou,
        "string",
        label="excess_pv_energy_use_in_tou",
    )
    state.huawei_batteries_excess_pv_use_in_tou = (
        str(_raw_excess) if _raw_excess is not None else None
    )
    _raw_wm = _read(
        cfg.huawei_solar_batteries_working_mode,
        "string",
        label="batteries_working_mode",
    )
    state.huawei_batteries_working_mode = str(_raw_wm) if _raw_wm is not None else None
    soc_pct = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_state_of_capacity,
            "float",
            label="state_of_capacity",
        )
    )
    if soc_pct is None:
        state.add_missing_entity(
            "Critical: battery SoC returned None (unavailable/invalid)"
        )
    state.huawei_batteries_soc_pct = soc_pct

    eod_soc = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_end_of_discharge_soc,
            "float",
            label="end_of_discharge_soc",
        )
    )
    # End-of-discharge SoC is non-critical — fall back to safe default of 5 %
    state.huawei_batteries_end_of_discharge_soc_pct = (
        eod_soc if eod_soc is not None else 5.0
    )

    state.huawei_batteries_charging_cutoff_capacity_pct = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_charging_cutoff_capacity,
            "float",
            label="charging_cutoff_capacity",
        )
    )

    state.huawei_batteries_grid_charge_cutoff_soc_pct = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_grid_charge_cutoff_soc,
            "float",
            label="grid_charge_cutoff_soc",
        )
    )

    max_charge_w = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_maximum_charging_power,
            "float",
            label="max_charging_power",
        )
    )
    if max_charge_w is None:
        state.add_missing_entity(
            "Critical: battery max charge power returned None (unavailable/invalid)"
        )
    state.huawei_batteries_max_charge_power_w = max_charge_w

    max_discharge_w = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_maximum_discharging_power,
            "float",
            label="max_discharging_power",
        )
    )
    if max_discharge_w is None:
        state.add_missing_entity(
            "Critical: battery max discharge power returned None (unavailable/invalid)"
        )
    state.huawei_batteries_max_discharge_power_w = max_discharge_w

    rated_capacity_wh = convert_to_float(
        _read(
            cfg.huawei_solar_batteries_rated_capacity,
            "float",
            label="batteries_rated_capacity_max",
        )
    )
    if rated_capacity_wh is None:
        state.add_missing_entity(
            "Critical: battery rated capacity returned None (unavailable/invalid)"
        )
    state.huawei_batteries_rated_capacity_wh = rated_capacity_wh
    _raw_apc = _read(
        cfg.huawei_solar_inverter_active_power_control,
        "string",
        label="inverter_active_power_control",
    )
    state.huawei_inverter_active_power_control = (
        str(_raw_apc) if _raw_apc is not None else None
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
        except (
            HomeAssistantError,
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
        ) as exc:
            state.add_missing_entity(
                f"Error reading TOU periods "
                f"(entity_id={cfg.huawei_solar_batteries_tou_charging_and_discharging_periods}): "
                f"{type(exc).__name__}: {exc}"
            )
            _LOGGER.warning(
                "TOU periods read failed (entity_id=%s): %s: %s",
                cfg.huawei_solar_batteries_tou_charging_and_discharging_periods,
                type(exc).__name__,
                repr(exc),
            )
    else:
        state.add_missing_entity("Missing entity: TOU periods")
    state.tou_periods = tou

    # --- Derived battery capacities ---
    _compute_battery_capacities(state)

    # --- Net consumption ---
    _compute_net_consumption(state, cfg)

    # --- EV planned load live state — primary EV ---
    if cfg.ev_planned_load_enabled:
        _read_ev_planned_load_state(sensor, state, cfg, _read, is_second=False)

    # --- EV planned load live state — second EV ---
    if cfg.ev_second_planned_load_enabled:
        _read_ev_planned_load_state(sensor, state, cfg, _read, is_second=True)

    # --- Register state-change listeners for reactive entities ---
    new_unsubs = await _register_listeners(sensor, cfg, state, tracked_entities)

    return state, fwm_entity, new_unsubs


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


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
    # Respect the max-SoC ceiling from the charging cutoff entity; default to 100 %
    # (no upper restriction) when the entity is unavailable.
    max_soc = state.huawei_batteries_charging_cutoff_capacity_pct or 100.0
    effective_max_soc = min(max(max_soc, eod_soc), 100.0)
    reserve_kwh = rated_kwh * (eod_soc / 100.0)
    max_kwh = rated_kwh * (effective_max_soc / 100.0)
    usable_kwh = max(max_kwh - reserve_kwh, 0.0)
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


def _read_ev_planned_load_state(
    sensor, state: LiveState, cfg, _read, is_second: bool
) -> None:
    """Read EV planned load live state into ``state`` for primary or second EV.

    Args:
        sensor: Working-mode sensor instance (provides ``hass``).
        state: :class:`LiveState` to update in place.
        cfg: Current sensor configuration.
        _read: Closure from ``async_collect_live_state`` for reading HA entities.
        is_second: When ``True``, read second-EV fields; otherwise read primary fields.
    """
    p = "ev_second_planned_load" if is_second else "ev_planned_load"
    ev_cfg = cfg.ev_second if is_second else cfg.ev

    # connected_sensor, soc_sensor, and target_soc_entity were removed from
    # the planned-load schema because they duplicate the basic EV charger
    # sensors (hsem_ev_connected, hsem_ev_soc, hsem_ev_soc_target).  The
    # state collector falls back to those here.
    connected_sensor = (
        getattr(cfg, f"{p}_connected_sensor", None) or ev_cfg.connected_entity
    )
    soc_sensor = getattr(cfg, f"{p}_soc_sensor", None) or ev_cfg.soc_entity
    target_soc_entity = (
        getattr(cfg, f"{p}_target_soc_entity", None) or ev_cfg.soc_target_entity
    )
    target_soc_fixed = getattr(cfg, f"{p}_target_soc_fixed", 80.0)
    smart_entity = getattr(cfg, f"{p}_smart_charging_entity", None)
    deadline_entity = getattr(cfg, f"{p}_deadline_entity", None)
    deadline_fixed = getattr(cfg, f"{p}_deadline_fixed", "07:00")

    if connected_sensor:
        setattr(
            state,
            f"{p}_connected",
            bool(
                convert_to_boolean(
                    _read(connected_sensor, "boolean", label=f"{p}_connected")
                )
            ),
        )
    else:
        # No connected sensor configured — assume EV is always connected so
        # the planner can still schedule charging based on SoC and deadline.
        setattr(state, f"{p}_connected", True)

    if soc_sensor:
        _soc = convert_to_float(_read(soc_sensor, "float", label=f"{p}_soc"))
        setattr(state, f"{p}_current_soc_pct", _soc if _soc is not None else 0.0)

    if target_soc_entity:
        _tsoc = convert_to_float(
            _read(target_soc_entity, "float", label=f"{p}_target_soc")
        )
        setattr(
            state,
            f"{p}_target_soc_pct",
            _tsoc if _tsoc is not None else target_soc_fixed,
        )
    else:
        setattr(state, f"{p}_target_soc_pct", target_soc_fixed)

    if smart_entity:
        _sc = convert_to_boolean(
            _read(smart_entity, "boolean", label=f"{p}_smart_charging")
        )
        setattr(
            state, f"{p}_smart_charging_enabled", bool(_sc) if _sc is not None else True
        )

    setattr(
        state,
        f"{p}_deadline",
        _resolve_ev_deadline_from_params(sensor, deadline_entity, deadline_fixed),
    )


def _resolve_ev_deadline_from_params(sensor, deadline_entity, deadline_fixed):
    """Resolve EV charging deadline from entity or fixed config string.

    Args:
        sensor: Working-mode sensor instance (provides ``hass``).
        deadline_entity: Optional HA entity whose state is a time string.
        deadline_fixed: Fallback HH:MM string from config.

    Returns:
        A timezone-aware ``datetime`` for the deadline, or ``None``.
    """
    import re
    from datetime import datetime as _dt
    from datetime import time as _time
    from datetime import timedelta

    time_str: str | None = None

    if deadline_entity:
        try:
            raw = ha_get_entity_state_and_convert(sensor, deadline_entity, None)
            from homeassistant.core import State as _State  # noqa: PLC0415

            if isinstance(raw, _State):
                time_str = raw.state
            elif isinstance(raw, str):
                time_str = raw
        except Exception:
            pass

    if not time_str:
        time_str = deadline_fixed or "07:00"

    m = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", (time_str or "").strip())
    if not m:
        return None

    hour, minute = int(m.group(1)), int(m.group(2))
    from custom_components.hsem.utils.datetime_utils import now as hsem_now

    now_local = hsem_now()
    today = now_local.date()
    deadline_naive = _dt.combine(today, _time(hour, minute))
    deadline = deadline_naive.replace(tzinfo=now_local.tzinfo)

    if deadline <= now_local:
        deadline = deadline + timedelta(days=1)

    return deadline


async def _register_listeners(
    sensor,
    cfg: SensorConfig,
    state: LiveState,
    tracked_entities: set[str],
) -> list:
    """Register ``async_track_state_change_event`` for reactive entities.

    Only entities that have not been tracked before are registered (idempotent).

    Returns:
        List of new unsubscribe callables for all listeners registered during
        this call.  The caller should store these and cancel them on teardown.
    """
    new_unsubs: list = []
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
            unsub = async_track_state_change_event(
                sensor.hass, [entity_id], sensor._async_handle_update
            )
            new_unsubs.append(unsub)
            tracked_entities.add(entity_id)

    return new_unsubs


# ---------------------------------------------------------------------------
# Single-pass state collection (snapshot-based pipeline)
# ---------------------------------------------------------------------------


async def async_collect_all_states(
    sensor,
    cfg: SensorConfig,
    force_working_mode_cache: str | None,
    tracked_entities: set[str],
    energy_average_entity_id_cache: dict[str, str] | None = None,
) -> tuple[StateSnapshot, str | None, list]:
    """Collect **all** HA states once into an immutable :class:`StateSnapshot`.

    This is the single entry point for the snapshot-based pipeline.  It replaces
    the three-stage read pattern (``async_collect_live_state`` → population reads)
    with a single pass over all required entities.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance (used for ``hass`` access,
            ``entity_id``, and :func:`async_logger`).
        cfg: Current sensor configuration.
        force_working_mode_cache: Previously resolved entity_id or ``None``.
        tracked_entities: Mutable set of entity_ids already registered for
            state-change tracking.  Updated in-place.
        energy_average_entity_id_cache: Optional mutable cache mapping
            unique_id → entity_id for energy average sensors.  If ``None``,
            a fresh cache is used.

    Returns:
        A ``(StateSnapshot, force_working_mode_entity_id, new_unsub_callbacks)``
        tuple.
    """
    # 1. Collect live entity states (battery, power, EV, etc.)
    live, fwm_entity, new_unsubs = await async_collect_live_state(
        sensor, cfg, force_working_mode_cache, tracked_entities
    )

    # 2. Pre-read energy average sensor values (24 hours × 4 periods)
    #    Gracefully skips unavailable sensors — the caller will detect
    #    missing data via the population functions and set missing_entities.
    avg_cache = energy_average_entity_id_cache or {}
    energy_average_values: dict[str, float] = {}

    for h in range(24):
        hour_end = (h + 1) % 24
        for days, _uid_key in [(1, "1d"), (3, "3d"), (7, "7d"), (14, "14d")]:
            uid = get_energy_average_sensor_unique_id(h, hour_end, days)
            if uid not in avg_cache:
                try:
                    eid = await async_resolve_entity_id_from_unique_id(sensor, uid)
                except Exception:
                    eid = None
                if eid is None:
                    # Registry lookup can fail on the first coordinator cycle
                    # if the sensor hasn't been registered yet.  The entity_id
                    # is deterministic from the unique_id — construct it
                    # directly so we can start collecting data immediately.
                    eid = get_energy_average_sensor_entity_id(h, hour_end, days)
                if eid is not None:
                    avg_cache[uid] = eid
            eid = avg_cache.get(uid)
            if eid:
                try:
                    val = convert_to_float(
                        ha_get_entity_state_and_convert(sensor, eid, "float", 3)
                    )
                    if val is not None:
                        energy_average_values[eid] = val
                except Exception:
                    # Entity unavailable — skip; downstream population will
                    # handle the gap by returning False (transient).
                    pass

    # 3. Pre-read EDS and Solcast sensor state objects for attribute access
    sensor_attributes: dict[str, dict] = {}
    for entity_id in (
        cfg.energi_data_service_import,
        cfg.energi_data_service_export,
        cfg.solcast_pv_forecast_forecast_today,
        cfg.solcast_pv_forecast_forecast_tomorrow,
    ):
        if entity_id is None:
            continue
        state_obj = sensor.hass.states.get(entity_id)
        if state_obj:
            # Only store attributes — the raw state value is not needed here
            # (the populator reads from attributes).
            sensor_attributes[entity_id] = dict(state_obj.attributes)

    snapshot = StateSnapshot(
        live=live,
        energy_average_values=energy_average_values,
        sensor_attributes=sensor_attributes,
    )

    return snapshot, fwm_entity, new_unsubs
