"""Price and Solcast PV population (async + snapshot).

Populates import/export price and Solcast PV estimate fields on
:class:`HourlyRecommendation` slots from HA sensor attributes (async)
or from a pre-collected :class:`StateSnapshot` (snapshot).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.datetime_utils import (
    normalize_datetime,
    normalize_slot_start,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

from . import _resolve_cached  # noqa: F401


async def async_populate_price_and_solcast(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    recommendations: list[HourlyRecommendation],
    cfg: SensorConfig,
) -> None:
    """Populate import/export prices and Solcast PV estimates into recommendation slots.

    Reads attribute arrays from the EDS and Solcast sensors, matches each data
    point to the corresponding :class:`HourlyRecommendation` by datetime, and
    writes the value into the appropriate field.

    The actual source interval (cadence of the data) is **auto-detected** from
    consecutive timestamps in each attribute array.  This means the planner
    correctly handles mixed-cadence sensors in the same sensor entity — for
    example an EDS sensor that has 15-min ``prices_today`` entries but only
    hourly ``forecast`` entries for tomorrow — without requiring any hardcoded
    per-attribute overrides.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance for HA access and logging.
        recommendations: Mutable list of recommendation slots to update.
        cfg: Current sensor configuration.
    """
    price_fallback = cfg.electricity_price_update_interval
    solcast_fallback = 60  # Solcast always publishes hourly totals

    # Import price — read from primary sensor (may embed forecast attributes)
    import_matched = await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.import_electricity_price_sensor,
        "import_price",
        cfg.solcast_pv_forecast_forecast_likelihood,
        price_fallback,
    )
    # Import price — fallback to dedicated forecast sensor if configured
    if cfg.import_electricity_price_forecast_sensor:
        import_matched += await _async_update_hourly_field(
            sensor,
            recommendations,
            cfg.import_electricity_price_forecast_sensor,
            "import_price",
            cfg.solcast_pv_forecast_forecast_likelihood,
            price_fallback,
        )
    if import_matched == 0:
        _LOGGER.warning(
            "No import price data matched from sensor(s) %s — "
            "planner will use 0.0 for all slots. "
            "Check that the sensor is available and its attribute format is supported.",
            cfg.import_electricity_price_sensor,
        )
    # Export price — read from primary sensor
    export_matched = await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.export_electricity_price_sensor,
        "export_price",
        cfg.solcast_pv_forecast_forecast_likelihood,
        price_fallback,
    )
    # Export price — fallback to dedicated forecast sensor
    if cfg.export_electricity_price_forecast_sensor:
        export_matched += await _async_update_hourly_field(
            sensor,
            recommendations,
            cfg.export_electricity_price_forecast_sensor,
            "export_price",
            cfg.solcast_pv_forecast_forecast_likelihood,
            price_fallback,
        )
    if export_matched == 0:
        _LOGGER.warning(
            "No export price data matched from sensor(s) %s — "
            "planner will use 0.0 for all slots. "
            "Check that the sensor is available and its attribute format is supported.",
            cfg.export_electricity_price_sensor,
        )
    # Solcast today
    solcast_today_matched = await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.solcast_pv_forecast_forecast_today,
        "solcast_pv_estimate_kwh",
        cfg.solcast_pv_forecast_forecast_likelihood,
        solcast_fallback,
    )
    if solcast_today_matched == 0:
        _LOGGER.debug(
            "No Solcast today data matched from sensor %s — "
            "PV estimates will be 0.0 for today.",
            cfg.solcast_pv_forecast_forecast_today,
        )
    # Solcast tomorrow
    solcast_tomorrow_matched = await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.solcast_pv_forecast_forecast_tomorrow,
        "solcast_pv_estimate_kwh",
        cfg.solcast_pv_forecast_forecast_likelihood,
        solcast_fallback,
    )
    if solcast_tomorrow_matched == 0:
        _LOGGER.debug(
            "No Solcast tomorrow data matched from sensor %s — "
            "PV estimates will be 0.0 for tomorrow.",
            cfg.solcast_pv_forecast_forecast_tomorrow,
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _detect_interval_minutes(
    entries: list[dict[str, Any]],
    time_key: str,
    fallback: int,
) -> int:
    """Detect the cadence of a data array by measuring gaps between timestamps.

    Parses up to the first 10 entries, collects the gaps between consecutive
    parseable timestamps, and returns the median gap rounded to the nearest
    whole minute.  Falls back to ``fallback`` when fewer than two entries can
    be parsed or the detected gap is not a positive integer.

    Args:
        entries: List of data-point dicts from a sensor attribute array.
        time_key: Dict key that holds the timestamp for each entry.
        fallback: Value to return when detection is not possible.

    Returns:
        Detected interval in whole minutes, or ``fallback``.
    """
    timestamps: list[datetime] = []
    for entry in entries[:10]:
        raw = entry.get(time_key)
        if not raw:
            continue
        try:
            dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
            timestamps.append(dt)
        except ValueError, TypeError:
            continue
        if len(timestamps) >= 5:
            break

    if len(timestamps) < 2:
        return fallback

    gaps_minutes = [
        (timestamps[i + 1] - timestamps[i]).total_seconds() / 60
        for i in range(len(timestamps) - 1)
        if timestamps[i + 1] > timestamps[i]
    ]
    if not gaps_minutes:
        return fallback

    # Use the median gap to be robust against out-of-order or duplicate entries.
    gaps_minutes.sort()
    median_gap = gaps_minutes[len(gaps_minutes) // 2]
    rounded = round(median_gap)
    return rounded if rounded > 0 else fallback


async def _async_update_hourly_field(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    recommendations: list[HourlyRecommendation],
    sensor_id: str | None,
    field_name: str,
    solcast_likelihood_key: str,
    fallback_interval_minutes: int,
) -> int:
    """Match sensor attribute data to recommendation slots and write one field.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance.
        recommendations: Mutable recommendation list.
        sensor_id: Entity ID to read attributes from, or None (no-op).
        field_name: Attribute name on :class:`HourlyRecommendation` to set.
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.
        fallback_interval_minutes: Interval used when auto-detection fails.

    Returns:
        Number of data points successfully matched to at least one slot.
    """
    if sensor_id is None:
        return 0

    sensor_state = sensor.hass.states.get(sensor_id)
    if not sensor_state:
        _LOGGER.debug(f"Input sensor {sensor_id} was not found for data.")
        return 0

    return _populate_from_attributes(
        sensor_state.attributes,
        recommendations,
        field_name,
        solcast_likelihood_key,
        fallback_interval_minutes,
    )


# ---------------------------------------------------------------------------
# Snapshot-based population (no HA state lookups)
# ---------------------------------------------------------------------------


def populate_price_and_solcast_from_snapshot(
    recommendations: list[HourlyRecommendation],
    snapshot: StateSnapshot,
    cfg: SensorConfig,
) -> None:
    """Populate prices and Solcast PV estimates using a pre-collected snapshot.

    Synchronous — no HA state lookups needed.  Uses :attr:`StateSnapshot.sensor_attributes`
    which was populated by :func:`~state_collector.async_collect_all_states`.

    Args:
        recommendations: Mutable list of recommendation slots to update.
        snapshot: Pre-collected state snapshot.
        cfg: Current sensor configuration.
    """
    price_fallback = cfg.electricity_price_update_interval
    solcast_fallback = 60

    import_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.import_electricity_price_sensor or ""),
        recommendations,
        "import_price",
        cfg.solcast_pv_forecast_forecast_likelihood,
        price_fallback,
    )
    if cfg.import_electricity_price_forecast_sensor:
        import_matched += _populate_from_attributes(
            snapshot.sensor_attributes.get(
                cfg.import_electricity_price_forecast_sensor or ""
            ),
            recommendations,
            "import_price",
            cfg.solcast_pv_forecast_forecast_likelihood,
            price_fallback,
        )
    if import_matched == 0:
        _LOGGER.warning(
            "No import price data matched from sensor(s) %s — "
            "planner will use 0.0 for all slots. "
            "Check that the sensor is available and its attribute format is supported.",
            cfg.import_electricity_price_sensor,
        )
    export_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.export_electricity_price_sensor or ""),
        recommendations,
        "export_price",
        cfg.solcast_pv_forecast_forecast_likelihood,
        price_fallback,
    )
    if cfg.export_electricity_price_forecast_sensor:
        export_matched += _populate_from_attributes(
            snapshot.sensor_attributes.get(
                cfg.export_electricity_price_forecast_sensor or ""
            ),
            recommendations,
            "export_price",
            cfg.solcast_pv_forecast_forecast_likelihood,
            price_fallback,
        )
    if export_matched == 0:
        _LOGGER.warning(
            "No export price data matched from sensor(s) %s — "
            "planner will use 0.0 for all slots. "
            "Check that the sensor is available and its attribute format is supported.",
            cfg.export_electricity_price_sensor,
        )
    solcast_today_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.solcast_pv_forecast_forecast_today or ""),
        recommendations,
        "solcast_pv_estimate_kwh",
        cfg.solcast_pv_forecast_forecast_likelihood,
        solcast_fallback,
    )
    if solcast_today_matched == 0:
        _LOGGER.debug(
            "No Solcast today data matched from sensor %s — "
            "PV estimates will be 0.0 for today.",
            cfg.solcast_pv_forecast_forecast_today,
        )
    solcast_tomorrow_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.solcast_pv_forecast_forecast_tomorrow or ""),
        recommendations,
        "solcast_pv_estimate_kwh",
        cfg.solcast_pv_forecast_forecast_likelihood,
        solcast_fallback,
    )
    if solcast_tomorrow_matched == 0:
        _LOGGER.debug(
            "No Solcast tomorrow data matched from sensor %s — "
            "PV estimates will be 0.0 for tomorrow.",
            cfg.solcast_pv_forecast_forecast_tomorrow,
        )


def _populate_from_attributes(
    attributes: dict[str, Any] | None,
    recommendations: list[HourlyRecommendation],
    field_name: str,
    solcast_likelihood_key: str,
    fallback_interval_minutes: int,
) -> int:
    """Match pre-read sensor attribute data to recommendation slots.

    For each recognised attribute key the actual data cadence is
    **auto-detected** from consecutive timestamps in that array.  This
    means the correct fan-out window is used even when different attribute
    keys on the same sensor publish at different intervals — for example
    ``prices_today`` at 15 min and ``forecast`` at 60 min on the same EDS
    sensor entity.

    The raw value from each data point is stored directly on the
    :class:`HourlyRecommendation` slot — no scaling is applied.
    ``coordinator_builder`` passes the value straight through to the planner.

    Args:
        attributes: The ``.attributes`` dict of the sensor, or ``None``.
        recommendations: Mutable recommendation list.
        field_name: Attribute name on :class:`HourlyRecommendation` to set.
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.
        fallback_interval_minutes: Interval assumed when auto-detection fails
            (fewer than 2 parseable timestamps in the array).

    Returns:
        Number of data points successfully matched to at least one slot.
    """
    if not attributes:
        return 0

    # Map each recognised sensor attribute key to the list of (time_key, value_key)
    # pairs that may appear in that attribute's entries.  Multiple pairs allow
    # one attribute to be matched regardless of which sensor integration
    # published it (e.g. EDS ``hour``/``price`` vs nordpool ``start``/``value``).
    data_sources: dict[str, list[dict[str, str]]] = {
        "forecast": [{"k": "hour", "v": "price"}],
        "raw_tomorrow": [
            {"k": "hour", "v": "price"},
            {"k": "start", "v": "value"},  # custom-components/nordpool
        ],
        "raw_today": [
            {"k": "hour", "v": "price"},
            {"k": "start", "v": "value"},  # custom-components/nordpool
        ],
        "prices": [
            {"k": "start", "v": "price"},
            {"k": "start_time", "v": "price"},  # Tibber Prices
        ],
        "prices_today": [
            {"k": "start", "v": "price"},
            {"k": "time", "v": "price"},
        ],
        "prices_tomorrow": [
            {"k": "start", "v": "price"},
            {"k": "time", "v": "price"},
        ],
        "detailedHourly": [{"k": "period_start", "v": solcast_likelihood_key}],
        "detailedForecast": [{"k": "period_start", "v": solcast_likelihood_key}],
        "data": [{"k": "start_time", "v": "price_per_kwh"}],
        # Amber Electric forecast sensor format
        "forecasts": [{"k": "start_time", "v": "per_kwh"}],
    }

    matched = 0
    for attr, kv_list in data_sources.items():
        sensor_data: list[dict[str, Any]] = attributes.get(attr) or []
        if not sensor_data:
            continue

        _LOGGER.debug("Updating data for %s from attribute %s...", field_name, attr)

        # Detect the actual cadence of this specific attribute array using
        # the first recognised time key.  Different attributes on the same
        # sensor can have different cadences (e.g. prices_today 15 min,
        # forecast 60 min), so detection must happen per-attribute.
        detected_interval = fallback_interval_minutes
        for kv in kv_list:
            detected = _detect_interval_minutes(sensor_data, kv["k"], 0)
            if detected > 0:
                detected_interval = detected
                break

        # Prices are rates (currency/kWh) — store the raw value unchanged.
        # Solcast PV is energy — the Solcast sensor publishes hourly kWh
        # totals; the per-slot fraction is computed in slot_population.py
        # via `pv_estimate / scale` (scale = 60 / slot_minutes), so
        # SolcastSlot.pv_estimate must hold the full hourly kWh.
        #
        # In both cases we store the raw value directly so that
        # coordinator_builder can pass it straight to the planner
        # without any additional multiply.  The old divide-then-multiply
        # round-trip is gone; with per-attribute auto-detection the raw
        # value is the only value that remains correct across mixed cadences.
        source_window = timedelta(minutes=detected_interval)

        for data in sensor_data:
            for kv in kv_list:
                raw_time = data.get(kv["k"])
                if not raw_time:
                    continue

                if isinstance(raw_time, datetime):
                    dt_key = raw_time
                else:
                    try:
                        dt_key = datetime.fromisoformat(str(raw_time))
                    except ValueError, TypeError:
                        continue

                try:
                    # Floor to the start of the detected source interval so
                    # that each data point is anchored at its slot boundary.
                    dt_key = normalize_slot_start(dt_key, detected_interval)
                except ValueError, OSError:
                    continue

                value = convert_to_float(data.get(kv["v"]))
                if value is None:
                    continue

                for obj in recommendations:
                    obj_start = normalize_datetime(obj.start)
                    if dt_key <= obj_start < dt_key + source_window:
                        setattr(obj, field_name, round(value, 5))
                        matched += 1

    return matched
