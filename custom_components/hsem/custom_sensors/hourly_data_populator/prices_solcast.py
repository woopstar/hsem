"""Price and Solcast PV population (async + snapshot).

Populates import/export price and Solcast PV estimate fields on
:class:`HourlyRecommendation` slots from HA sensor attributes (async)
or from a pre-collected :class:`StateSnapshot` (snapshot).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.hsem.models.forecast_coverage import ForecastCoverage
from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot
from custom_components.hsem.utils.conversion import convert_to_float
from custom_components.hsem.utils.datetime_utils import (
    normalize_slot_start,
    utc_key,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

#: Attributes that hold predicted prices, not prices the market published:
#: ``forecast`` (Energi Data Service, Carnot), ``predictions`` (Open Spot
#: Forecast) and ``forecasts`` (Amber Electric).  Their values price a slot
#: as before, and never enter the price history or the held prices
#: (issue #1434).
PREDICTION_ATTRIBUTES = frozenset({"forecast", "predictions", "forecasts"})

#: Attributes that always hold published prices: the day-ahead prices of
#: Energi Data Service and Nord Pool.  No sensor state makes them a
#: prediction (issue #1452).
PUBLISHED_ATTRIBUTES = frozenset({"raw_today", "raw_tomorrow"})

#: Day-ahead prices are published for today and tomorrow.  A price series
#: that still has an entry this long after the first slot of the cycle (local
#: midnight today) reaches past tomorrow and holds predictions (issue #1452).
#: The 12 hours on top of the two days allow for a market day in another
#: time zone than the local one.
_PUBLISHED_HORIZON = timedelta(hours=60)
_ALWAYS = datetime.max.replace(tzinfo=UTC)

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


def _overlap_weighted_mean(covering: list[tuple[float, float]]) -> float:
    """Return the overlap-weighted mean of the data points covering a slot.

    A single covering point is returned as is, so a source at or above the
    slot width stores exactly the value it published.

    Args:
        covering: ``(value, overlap seconds)`` of each data point that
            overlaps the slot; never empty, overlaps strictly positive.

    Returns:
        The mean value, weighted by how long each point covers the slot.
    """
    if len(covering) == 1:
        return covering[0][0]
    covered_seconds = math.fsum(seconds for _, seconds in covering)
    return math.fsum(value * seconds for value, seconds in covering) / covered_seconds


def _attribute_formats(solcast_likelihood_key: str) -> dict[str, list[dict[str, str]]]:
    """Return the recognised forecast attributes and their entry formats.

    Args:
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.

    Returns:
        Each recognised attribute key mapped to the ``(time_key, value_key)``
        pairs, as ``{"k": ..., "v": ...}``, its entries may use.
    """
    # Map each recognised sensor attribute key to the list of (time_key, value_key)
    # pairs that may appear in that attribute's entries.  Multiple pairs allow
    # one attribute to be matched regardless of which sensor integration
    # published it (e.g. EDS ``hour``/``price`` vs nordpool ``start``/``value``).
    return {
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
            {"k": "start", "v": "price"},  # Strømligning
            {"k": "start_time", "v": "price"},  # Tibber Prices
        ],
        # Open Spot Forecast "Price Forecast (ML)", detailed or compact layout
        "predictions": [{"k": "start", "v": "price"}],
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


def _entry_start(raw_time: Any, interval_minutes: int) -> datetime | None:
    """Return the UTC start of a data point's source interval.

    Args:
        raw_time: The entry's time value: a datetime or an ISO string.
        interval_minutes: Cadence of the source, to floor the time to.

    Returns:
        The start of the source interval the time falls in, or ``None`` when
        the value is missing or cannot be read as a time.
    """
    if not raw_time:
        return None
    if isinstance(raw_time, datetime):
        dt_key = raw_time
    else:
        try:
            dt_key = datetime.fromisoformat(str(raw_time))
        except ValueError, TypeError:
            return None
    try:
        # Floor to the start of the detected source interval so that each
        # data point is anchored at its slot boundary.  Match by physical
        # instant: two HA-local datetimes compare by wall clock, which cannot
        # tell the DST fall-back hour's two occurrences apart (issue #1160).
        return utc_key(normalize_slot_start(dt_key, interval_minutes))
    except ValueError, OSError:
        return None


def _published_until(
    attr: str,
    attributes: dict[str, Any],
    starts: list[datetime],
    first_slot: datetime,
) -> datetime | None:
    """Return the time before which an attribute's entries are real prices.

    The same attribute name holds published prices in one integration and
    predictions in another (``docs/price-attributes.md``), so the sensor's
    own state decides:

    * An attribute in :data:`PUBLISHED_ATTRIBUTES` holds published prices,
      whatever else the sensor says.
    * An attribute in :data:`PREDICTION_ATTRIBUTES` holds predictions.
    * ``forecast_data`` set: Strømligning's tomorrow sensors, while tomorrow
      is a forecast.
    * ``known_until``: Open Spot Forecast marks where its confirmed prices
      end and its forecast begins.
    * A series that reaches past tomorrow cannot be day-ahead prices alone,
      and nothing in it tells the two apart: EPEX Spot with the
      Energyforecast source, Strømligning's forecast sensors.

    Args:
        attr: The attribute name.
        attributes: The sensor's attributes.
        starts: UTC start of every entry of the attribute.
        first_slot: UTC start of the cycle's first slot.

    Returns:
        ``None`` when every entry is a prediction, otherwise the UTC time an
        entry must start before to count as published.
    """
    if attr in PUBLISHED_ATTRIBUTES:
        return _ALWAYS
    if attr in PREDICTION_ATTRIBUTES or attributes.get("forecast_data") is True:
        return None
    known_until = attributes.get("known_until")
    if known_until is not None:
        return _entry_start(known_until, 1)
    if any(start >= first_slot + _PUBLISHED_HORIZON for start in starts):
        return None
    return _ALWAYS


def _attribute_entries(value: Any) -> list[dict[str, Any]]:
    """Return a forecast attribute's value as one dict per data point.

    Most integrations publish a list of dicts, which is returned as is.
    Open Spot Forecast's compact layout is a dict of parallel arrays: ``s``
    (unix start seconds) and ``t`` (prices).  It is turned into
    ``{"start", "price"}`` entries so it is matched like the detailed layout.

    Args:
        value: The attribute's value, or ``None``.

    Returns:
        The data points; empty when the value holds none.
    """
    if isinstance(value, dict):
        return [
            {"start": datetime.fromtimestamp(start, tz=UTC), "price": price}
            for start, price in zip(
                value.get("s") or [], value.get("t") or [], strict=False
            )
            if isinstance(start, (int, float))
        ]
    return value or []


def _flat_price(snapshot: StateSnapshot, entity_id: str | None) -> float | None:
    """Return a price sensor's state when it is a flat price (issue #978).

    A flat-tariff sensor publishes its price as the state and no forecast
    attribute.  A sensor with any recognised forecast attribute is a forecast
    source even when that attribute is empty or stale, so its gaps stay gaps.

    Args:
        snapshot: Pre-collected state snapshot.
        entity_id: The configured price sensor, or ``None``.

    Returns:
        The state as a finite float, or ``None`` when the sensor is not
        configured, has a forecast attribute, or its state is not a number
        (unavailable, unknown, NaN or infinite).
    """
    if not entity_id:
        return None
    attributes = snapshot.sensor_attributes.get(entity_id) or {}
    if any(key in attributes for key in _attribute_formats("")):
        return None
    value = convert_to_float(snapshot.sensor_states.get(entity_id))
    if value is None or not math.isfinite(value):
        return None
    return value


def _populate_flat_price(
    snapshot: StateSnapshot,
    entity_id: str | None,
    recommendations: list[HourlyRecommendation],
    field_name: str,
    covered: set[datetime],
) -> float | None:
    """Write a flat-tariff sensor's state to every slot and count it as covered.

    Args:
        snapshot: Pre-collected state snapshot.
        entity_id: The configured price sensor, or ``None``.
        recommendations: Mutable recommendation list.
        field_name: Price field on :class:`HourlyRecommendation` to set.
        covered: Receives the UTC start of every slot written.

    Returns:
        The flat price written, or ``None`` when the sensor is not a flat
        price (see :func:`_flat_price`) and nothing was written.
    """
    value = _flat_price(snapshot, entity_id)
    if value is None:
        return None
    for obj in recommendations:
        setattr(obj, field_name, round(value, 5))
        covered.add(utc_key(obj.start))
    return value


# ---------------------------------------------------------------------------
# Snapshot-based population (no HA state lookups)
# ---------------------------------------------------------------------------


def populate_price_and_solcast_from_snapshot(
    recommendations: list[HourlyRecommendation],
    snapshot: StateSnapshot,
    cfg: SensorConfig,
) -> ForecastCoverage:
    """Populate prices and Solcast PV estimates using a pre-collected snapshot.

    Synchronous — no HA state lookups needed.  Uses :attr:`StateSnapshot.sensor_attributes`
    which was populated by :func:`~state_collector.async_collect_all_states`.

    Args:
        recommendations: Mutable list of recommendation slots to update.
        snapshot: Pre-collected state snapshot.
        cfg: Current sensor configuration.

    Returns:
        Which slots each source wrote to (issue #1196).  A slot a source did
        not cover keeps its ``0.0`` default on the recommendation; the
        coverage is what tells that default from a published ``0.0``.
        A price sensor without a forecast attribute and with a numeric
        state is a flat price and covers every slot (issue #978).
    """
    price_fallback = cfg.electricity_price_update_interval
    solcast_fallback = 60
    import_covered: set[datetime] = set()
    export_covered: set[datetime] = set()
    pv_covered: set[datetime] = set()

    # A flat-tariff sensor goes first so a forecast sensor's points still
    # take precedence on the slots they cover (issue #978).
    import_flat = _populate_flat_price(
        snapshot,
        cfg.import_electricity_price_sensor,
        recommendations,
        "import_price",
        import_covered,
    )
    # What the price sensor itself published as real prices: a flat price or
    # an attribute that is not a prediction.  Only this enters the price
    # history and the held prices (#1434).
    published_import = _covered_values(recommendations, "import_price", import_covered)
    import_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.import_electricity_price_sensor or ""),
        recommendations,
        "import_price",
        cfg.solcast_pv_forecast_forecast_likelihood,
        price_fallback,
        covered=import_covered,
        published=published_import,
    )
    import_predictions_only = import_matched > 0 and not published_import
    if cfg.import_electricity_price_forecast_sensor:
        import_matched += _populate_from_attributes(
            snapshot.sensor_attributes.get(
                cfg.import_electricity_price_forecast_sensor or ""
            ),
            recommendations,
            "import_price",
            cfg.solcast_pv_forecast_forecast_likelihood,
            price_fallback,
            covered=import_covered,
        )
    if import_flat is not None:
        _LOGGER.debug(
            "Import price sensor %s publishes no forecast attribute — "
            "using its state %s as a flat price for every slot.",
            cfg.import_electricity_price_sensor,
            import_flat,
        )
    elif import_matched == 0:
        _LOGGER.warning(
            "No import price data matched from sensor(s) %s — no slot has a "
            "price pair, so the planner reports every hour as missing and "
            "plans from held and stored prices, or at 0.0 without a price "
            "history. Check that the sensor is "
            "available and publishes either a supported forecast attribute "
            "or, for a flat price, a numeric state and no forecast attribute.",
            cfg.import_electricity_price_sensor,
        )
    export_flat = _populate_flat_price(
        snapshot,
        cfg.export_electricity_price_sensor,
        recommendations,
        "export_price",
        export_covered,
    )
    published_export = _covered_values(recommendations, "export_price", export_covered)
    export_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.export_electricity_price_sensor or ""),
        recommendations,
        "export_price",
        cfg.solcast_pv_forecast_forecast_likelihood,
        price_fallback,
        covered=export_covered,
        published=published_export,
    )
    export_predictions_only = export_matched > 0 and not published_export
    if cfg.export_electricity_price_forecast_sensor:
        export_matched += _populate_from_attributes(
            snapshot.sensor_attributes.get(
                cfg.export_electricity_price_forecast_sensor or ""
            ),
            recommendations,
            "export_price",
            cfg.solcast_pv_forecast_forecast_likelihood,
            price_fallback,
            covered=export_covered,
        )
    if export_flat is not None:
        _LOGGER.debug(
            "Export price sensor %s publishes no forecast attribute — "
            "using its state %s as a flat price for every slot.",
            cfg.export_electricity_price_sensor,
            export_flat,
        )
    elif export_matched == 0:
        _LOGGER.warning(
            "No export price data matched from sensor(s) %s — no slot has a "
            "price pair, so the planner reports every hour as missing and "
            "plans from held and stored prices, or at 0.0 without a price "
            "history. Check that the sensor is "
            "available and publishes either a supported forecast attribute "
            "or, for a flat price, a numeric state and no forecast attribute.",
            cfg.export_electricity_price_sensor,
        )
    solcast_today_matched = _populate_from_attributes(
        snapshot.sensor_attributes.get(cfg.solcast_pv_forecast_forecast_today or ""),
        recommendations,
        "solcast_pv_estimate_kwh",
        cfg.solcast_pv_forecast_forecast_likelihood,
        solcast_fallback,
        mean_over_slot=True,
        covered=pv_covered,
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
        mean_over_slot=True,
        covered=pv_covered,
    )
    if solcast_tomorrow_matched == 0:
        _LOGGER.debug(
            "No Solcast tomorrow data matched from sensor %s — "
            "PV estimates will be 0.0 for tomorrow.",
            cfg.solcast_pv_forecast_forecast_tomorrow,
        )
    return ForecastCoverage(
        import_price=frozenset(import_covered),
        export_price=frozenset(export_covered),
        pv=frozenset(pv_covered),
        pv_source_configured=bool(
            cfg.solcast_pv_forecast_forecast_today
            or cfg.solcast_pv_forecast_forecast_tomorrow
        ),
        published_import=published_import,
        published_export=published_export,
        import_predictions_only=import_predictions_only,
        export_predictions_only=export_predictions_only,
    )


def _covered_values(
    recommendations: list[HourlyRecommendation],
    field_name: str,
    covered: set[datetime],
) -> dict[datetime, float]:
    """Return the value of *field_name* on every covered slot, by UTC start."""
    return {
        key: float(getattr(rec, field_name))
        for rec in recommendations
        if (key := utc_key(rec.start)) in covered
    }


def _populate_from_attributes(
    attributes: dict[str, Any] | None,
    recommendations: list[HourlyRecommendation],
    field_name: str,
    solcast_likelihood_key: str,
    fallback_interval_minutes: int,
    *,
    mean_over_slot: bool = False,
    covered: set[datetime] | None = None,
    published: dict[datetime, float] | None = None,
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

    By default a slot takes the data point whose window contains the slot's
    start.  With *mean_over_slot* a slot instead takes the time-weighted mean
    of every data point that overlaps it, which is what an average-power
    series such as Solcast PV needs when the source is finer than the slot
    (issue #1191): a 60-minute slot under a half-hourly source would otherwise
    keep the first half-hour and drop the second.  A source at or above the
    slot width overlaps each slot with one point, so the value is unchanged.

    Args:
        attributes: The ``.attributes`` dict of the sensor, or ``None``.
        recommendations: Mutable recommendation list.
        field_name: Attribute name on :class:`HourlyRecommendation` to set.
        solcast_likelihood_key: Attribute key for Solcast PV estimate field.
        fallback_interval_minutes: Interval assumed when auto-detection fails
            (fewer than 2 parseable timestamps in the array).
        mean_over_slot: Store the overlap-weighted mean of the data points
            covering each slot instead of the point at the slot's start.
        covered: When given, receives the UTC start of every slot a value
            was written to (issue #1196).  A slot that is not in it kept the
            default it was created with.
        published: When given, receives the value of every slot written from
            an attribute that is not in :data:`PREDICTION_ATTRIBUTES`, by UTC
            start (issue #1434), and only from entries that
            :func:`_published_until` counts as published (issue #1452).  A
            prediction written to the same slot later does not change it.
            Not filled with *mean_over_slot*.

    Returns:
        Number of data points successfully matched to at least one slot.
    """
    if not attributes:
        return 0

    data_sources = _attribute_formats(solcast_likelihood_key)

    matched = 0
    for attr, kv_list in data_sources.items():
        sensor_data = _attribute_entries(attributes.get(attr))
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
        # Solcast PV is average power over the source period (kW); the unit
        # is defined on SolcastSlot.  The planner turns it into energy per
        # slot (kW × slot hours) in slot_population.py.
        #
        # In both cases we store the raw value directly so that
        # coordinator_builder can pass it straight to the planner
        # without any additional multiply.  The old divide-then-multiply
        # round-trip is gone; with per-attribute auto-detection the raw
        # value is the only value that remains correct across mixed cadences.
        source_window = timedelta(minutes=detected_interval)
        entries = [
            (window_start, value)
            for data in sensor_data
            for kv in kv_list
            if (window_start := _entry_start(data.get(kv["k"]), detected_interval))
            is not None
            and (value := convert_to_float(data.get(kv["v"]))) is not None
        ]
        published_until = (
            _published_until(
                attr,
                attributes,
                [start for start, _ in entries],
                utc_key(recommendations[0].start),
            )
            if published is not None and recommendations
            else None
        )
        # Per slot index: (value, overlap seconds) of each covering data point.
        overlaps: dict[int, list[tuple[float, float]]] = {}

        for window_start, value in entries:
            window_end = window_start + source_window
            for index, obj in enumerate(recommendations):
                obj_start = utc_key(obj.start)
                if mean_over_slot:
                    overlap_seconds = (
                        min(window_end, utc_key(obj.end)) - max(window_start, obj_start)
                    ).total_seconds()
                    if overlap_seconds > 0:
                        overlaps.setdefault(index, []).append((value, overlap_seconds))
                        matched += 1
                elif window_start <= obj_start < window_end:
                    setattr(obj, field_name, round(value, 5))
                    matched += 1
                    if covered is not None:
                        covered.add(obj_start)
                    if (
                        published is not None
                        and published_until is not None
                        and window_start < published_until
                    ):
                        published[obj_start] = round(value, 5)

        for index, covering in overlaps.items():
            setattr(
                recommendations[index],
                field_name,
                round(_overlap_weighted_mean(covering), 5),
            )
            if covered is not None:
                covered.add(utc_key(recommendations[index].start))

    return matched
