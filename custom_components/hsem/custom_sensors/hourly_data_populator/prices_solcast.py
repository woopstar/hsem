"""Price and Solcast PV population (async + snapshot).

Populates import/export price and Solcast PV estimate fields on
:class:`HourlyRecommendation` slots from HA sensor attributes (async)
or from a pre-collected :class:`StateSnapshot` (snapshot).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.datetime_utils import normalize_datetime
from custom_components.hsem.utils.logger import async_logger

from . import _resolve_cached  # noqa: F401 — shared helper, imported per package spec


async def async_populate_price_and_solcast(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    recommendations: list[HourlyRecommendation],
    cfg: SensorConfig,
) -> None:
    """Populate import/export prices and Solcast PV estimates into recommendation slots.

    Reads attribute arrays from the EDS and Solcast sensors, matches each data
    point to the corresponding :class:`HourlyRecommendation` by datetime, and
    writes the value into the appropriate field.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance for HA access and logging.
        recommendations: Mutable list of recommendation slots to update.
        cfg: Current sensor configuration.
    """
    # ---------------------------------------------------------------------------
    # Price interval semantics
    # ---------------------------------------------------------------------------
    # Prices are a *rate* (currency/kWh), not an energy quantity, so they
    # must NOT be summed across slots.  However, the price sensor publishes
    # one value per update interval (15, 30, or 60 min) while HSEM may plan
    # at a finer resolution (e.g. 15-min slots inside a 60-min interval).
    #
    # `price_share` converts between the two resolutions:
    #
    #   price_share = electricity_price_update_interval / recommendation_interval_minutes
    #
    # In `_async_update_hourly_field` (below) each raw price value is divided by
    # `price_share` before writing to the per-slot recommendation object.  This
    # stores the price *scaled to one recommendation slot's share* of the price
    # update interval.
    #
    # The inverse multiply (`rec.import_price * price_share`) is applied later in
    # `coordinator_builder.build_planner_input` to recover the original price rate
    # before passing it to the planner engine.
    #
    # Common configurations and their price_share values:
    #   Price 60 min  / slots 15 min  →  price_share = 4.0
    #   Price 30 min  / slots 15 min  →  price_share = 2.0
    #   Price 15 min  / slots 15 min  →  price_share = 1.0  (no scaling)
    #   Price 60 min  / slots 60 min  →  price_share = 1.0  (no scaling)
    price_share = (
        cfg.electricity_price_update_interval / cfg.recommendation_interval_minutes
    )
    # Solcast forecasts are always given as hourly totals (Wh/h), so the share
    # factor is always relative to 60 minutes regardless of price configuration.
    solcast_share = 60.0 / cfg.recommendation_interval_minutes

    # Import price — read from primary sensor (may embed forecast attributes)
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.import_electricity_price_sensor,
        "import_price",
        price_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )
    # Import price — fallback to dedicated forecast sensor if configured
    if cfg.import_electricity_price_forecast_sensor:
        await _async_update_hourly_field(
            sensor,
            recommendations,
            cfg.import_electricity_price_forecast_sensor,
            "import_price",
            price_share,
            cfg.solcast_pv_forecast_forecast_likelihood,
        )
    # Export price — read from primary sensor
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.export_electricity_price_sensor,
        "export_price",
        price_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )
    # Export price — fallback to dedicated forecast sensor if configured
    if cfg.export_electricity_price_forecast_sensor:
        await _async_update_hourly_field(
            sensor,
            recommendations,
            cfg.export_electricity_price_forecast_sensor,
            "export_price",
            price_share,
            cfg.solcast_pv_forecast_forecast_likelihood,
        )
    # Solcast today
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.solcast_pv_forecast_forecast_today,
        "solcast_pv_estimate_kwh",
        solcast_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )
    # Solcast tomorrow
    await _async_update_hourly_field(
        sensor,
        recommendations,
        cfg.solcast_pv_forecast_forecast_tomorrow,
        "solcast_pv_estimate_kwh",
        solcast_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _async_update_hourly_field(
    sensor: Any,  # NOSONAR -- HA internal type; circular import risk
    recommendations: list[HourlyRecommendation],
    sensor_id: str | None,
    field_name: str,
    share: float,
    solcast_likelihood_key: str,
) -> None:
    """Match sensor attribute data to recommendation slots and write one field.

    Args:
        sensor: The ``HSEMWorkingModeSensor`` instance.
        recommendations: Mutable recommendation list.
        sensor_id: Entity ID to read attributes from, or None (no-op).
        field_name: Attribute name on :class:`HourlyRecommendation` to set.
        share: Divisor applied to each raw value (accounts for sub-hourly slots).
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.
    """
    if sensor_id is None:
        return

    sensor_state = sensor.hass.states.get(sensor_id)
    if not sensor_state:
        await async_logger(sensor, f"Input sensor {sensor_id} was not found for data.")
        return

    # Each source exposes a different attribute key / time-key / value-key
    data_sources: dict[str, list[dict[str, str]]] = {
        "forecast": [{"k": "hour", "v": "price"}],
        "raw_tomorrow": [{"k": "hour", "v": "price"}],
        "raw_today": [{"k": "hour", "v": "price"}],
        "prices": [{"k": "start", "v": "price"}],
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
        # Amber Electric forecast sensor format: forecasts array on the forecast sensor
        "forecasts": [{"k": "start_time", "v": "per_kwh"}],
    }

    for attr, kv_list in data_sources.items():
        sensor_data = sensor_state.attributes.get(attr) or []
        if not sensor_data:
            continue

        await async_logger(sensor, f"Updating data for {field_name}...")

        for data in sensor_data:
            for kv in kv_list:
                raw_time = data.get(kv["k"])
                if not raw_time:
                    continue

                if isinstance(raw_time, datetime):
                    dt_key = raw_time
                else:
                    dt_key = datetime.fromisoformat(str(raw_time))

                try:
                    # Normalize to HA-local timezone, strip sub-minute precision
                    dt_key = normalize_datetime(dt_key).replace(minute=0, second=0)
                except ValueError, OSError:  # noqa: TRY302
                    # Skip data points with unparseable or non-local timestamps
                    continue

                value = convert_to_float(data.get(kv["v"]))
                if value is None:
                    continue

                # Scale raw value down to one recommendation-slot's share of the
                # source update interval.
                #
                # For prices:   share = price_share (price interval / slot interval)
                #   Price 60 min / slot 15 min → share=4 → store price/4 per slot
                #   Price 15 min / slot 15 min → share=1 → store price unchanged
                #
                # For Solcast PV:   share = solcast_share (60 / slot interval)
                #   60-min hourly forecast / slot 15 min → share=4 → store Wh/4 per slot
                #   60-min hourly forecast / slot 60 min → share=1 → store Wh unchanged
                #
                # The coordinator's `build_planner_input` applies the inverse multiply
                # (×price_share) before handing prices/PV to the planner engine, so the
                # divide here and the multiply there cancel exactly and the planner always
                # receives the original hourly-equivalent rate or energy quantity.
                value = value / share

                for obj in recommendations:
                    obj_hour = normalize_datetime(obj.start).replace(minute=0, second=0)
                    if obj.start.date() == dt_key.date() and obj_hour == dt_key:
                        setattr(obj, field_name, round(value, 5))


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
    price_share = (
        cfg.electricity_price_update_interval / cfg.recommendation_interval_minutes
    )
    solcast_share = 60.0 / cfg.recommendation_interval_minutes

    _update_hourly_field_from_attrs(
        recommendations,
        snapshot.sensor_attributes.get(cfg.import_electricity_price_sensor or ""),
        "import_price",
        price_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )
    if cfg.import_electricity_price_forecast_sensor:
        _update_hourly_field_from_attrs(
            recommendations,
            snapshot.sensor_attributes.get(
                cfg.import_electricity_price_forecast_sensor or ""
            ),
            "import_price",
            price_share,
            cfg.solcast_pv_forecast_forecast_likelihood,
        )
    _update_hourly_field_from_attrs(
        recommendations,
        snapshot.sensor_attributes.get(cfg.export_electricity_price_sensor or ""),
        "export_price",
        price_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )
    if cfg.export_electricity_price_forecast_sensor:
        _update_hourly_field_from_attrs(
            recommendations,
            snapshot.sensor_attributes.get(
                cfg.export_electricity_price_forecast_sensor or ""
            ),
            "export_price",
            price_share,
            cfg.solcast_pv_forecast_forecast_likelihood,
        )
    _update_hourly_field_from_attrs(
        recommendations,
        snapshot.sensor_attributes.get(cfg.solcast_pv_forecast_forecast_today or ""),
        "solcast_pv_estimate_kwh",
        solcast_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )
    _update_hourly_field_from_attrs(
        recommendations,
        snapshot.sensor_attributes.get(cfg.solcast_pv_forecast_forecast_tomorrow or ""),
        "solcast_pv_estimate_kwh",
        solcast_share,
        cfg.solcast_pv_forecast_forecast_likelihood,
    )


def _update_hourly_field_from_attrs(
    recommendations: list[HourlyRecommendation],
    attributes: dict[str, Any] | None,
    field_name: str,
    share: float,
    solcast_likelihood_key: str,
) -> None:
    """Match pre-read sensor attribute data to recommendation slots.

    This is the snapshot-based counterpart of ``_async_update_hourly_field``.
    Instead of calling ``hass.states.get()``, it operates on the
    ``attributes`` dict that was pre-read during snapshot collection.

    Args:
        recommendations: Mutable recommendation list.
        attributes: The ``.attributes`` dict of the sensor, or ``None``.
        field_name: Attribute name on :class:`HourlyRecommendation` to set.
        share: Divisor applied to each raw value.
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.
    """
    if attributes is None:
        return

    data_sources: dict[str, list[dict[str, str]]] = {
        "forecast": [{"k": "hour", "v": "price"}],
        "raw_tomorrow": [{"k": "hour", "v": "price"}],
        "raw_today": [{"k": "hour", "v": "price"}],
        "prices": [{"k": "start", "v": "price"}],
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
        # Amber Electric forecast sensor format: forecasts array on the forecast sensor
        "forecasts": [{"k": "start_time", "v": "per_kwh"}],
    }

    for attr, kv_list in data_sources.items():
        sensor_data = attributes.get(attr) or []
        if not sensor_data:
            continue

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
                    dt_key = normalize_datetime(dt_key).replace(minute=0, second=0)
                except ValueError, OSError:
                    continue

                value = convert_to_float(data.get(kv["v"]))
                if value is None:
                    continue

                value = value / share

                for obj in recommendations:
                    obj_hour = normalize_datetime(obj.start).replace(minute=0, second=0)
                    if obj.start.date() == dt_key.date() and obj_hour == dt_key:
                        setattr(obj, field_name, round(value, 5))
