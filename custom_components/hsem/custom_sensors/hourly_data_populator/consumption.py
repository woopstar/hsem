"""House consumption average population (async + snapshot).

Populates per-slot weighted average house consumption fields on
:class:`HourlyRecommendation` slots from HA energy average sensors (async)
or from a pre-collected :class:`StateSnapshot` (snapshot).

Missing hour blocks (issue #1110): a block the average sensors have never
stored a sample for is ``unavailable``. While the rolling window is young
each hour has at most one sample, so a single lost block used to block the
whole plan for a day. Up to :data:`MAX_ESTIMATED_LOAD_HOURS` such hours are
now filled with a conservative estimate: per window, the larger value of the
nearest measured hour on each side. It is never zero and never below a
measured neighbour. More missing hours still fail closed.
"""

from __future__ import annotations

from dataclasses import dataclass

from custom_components.hsem.models.hourly_recommendation import HourlyRecommendation
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.models.state_snapshot import StateSnapshot

# Delegate the spike-aware weighting algorithm to the canonical implementation
# in planner.slot_population so the logic lives in exactly one place.
from custom_components.hsem.planner.slot_population import weighted_avg_consumption
from custom_components.hsem.utils.logger import log_planner
from custom_components.hsem.utils.sensornames.energy import (
    get_energy_average_sensor_unique_id,
)

#: Maximum number of hour blocks without any stored sample that may be
#: filled with a conservative neighbour estimate (issue #1110). Above this
#: the profile is too thin to plan on and population fails closed.
MAX_ESTIMATED_LOAD_HOURS = 4

_WindowValues = tuple[float, float, float, float]


@dataclass(frozen=True)
class ConsumptionPopulation:
    """Outcome of one house-consumption population pass.

    Attributes:
        ok: True when every slot received a load estimate.
        missing_hours: Hours (0-23) whose average sensors reported no value.
        estimated_hours: Missing hours that were filled with the neighbour
            estimate. Empty when ``ok`` is False.
    """

    ok: bool
    missing_hours: tuple[int, ...] = ()
    estimated_hours: tuple[int, ...] = ()


def estimate_missing_hours(
    measured: dict[int, _WindowValues], missing: list[int]
) -> dict[int, _WindowValues]:
    """Fill missing hours with the per-window max of the nearest measured hours.

    The day is treated as a circle, so hour 0 neighbours hour 23. Taking the
    larger neighbour errs towards reserving battery energy for house load.

    Args:
        measured: Window values ``(1d, 3d, 7d, 14d)`` per measured hour.
        missing: Hours to estimate.

    Returns:
        Estimated window values per missing hour, or an empty dict when no
        hour was measured at all.
    """
    if not measured:
        return {}
    estimates: dict[int, _WindowValues] = {}
    for hour in missing:
        before = next(
            measured[(hour - step) % 24]
            for step in range(1, 25)
            if (hour - step) % 24 in measured
        )
        after = next(
            measured[(hour + step) % 24]
            for step in range(1, 25)
            if (hour + step) % 24 in measured
        )
        estimates[hour] = (
            max(before[0], after[0]),
            max(before[1], after[1]),
            max(before[2], after[2]),
            max(before[3], after[3]),
        )
    return estimates


# ---------------------------------------------------------------------------
# Snapshot-based average consumption population
# ---------------------------------------------------------------------------


def populate_avg_house_consumption_from_snapshot(
    recommendations: list[HourlyRecommendation],
    snapshot: StateSnapshot,
    cfg: SensorConfig,
    energy_average_entity_id_cache: dict[str, str],
    entry_id: str,
) -> ConsumptionPopulation:
    """Populate per-slot house consumption averages from a pre-collected snapshot.

    Synchronous — uses :attr:`StateSnapshot.energy_average_values` which was
    populated by :func:`~state_collector.async_collect_all_states`.

    The energy average sensors are HSEM's own entities.  When they are not yet
    registered (e.g. during the very first coordinator cycle) the function
    fails closed.  The caller **must not** treat this as a
    ``missing_input_entities`` error — it is a transient condition that
    resolves on the next cycle once the sensors are available.

    Every hour is inspected; an hour whose sensors report no value is
    collected in ``missing_hours``.  Up to :data:`MAX_ESTIMATED_LOAD_HOURS`
    missing hours are filled via :func:`estimate_missing_hours` (issue #1110).

    Args:
        recommendations: Mutable list of recommendation slots to update.
        snapshot: Pre-collected state snapshot with ``energy_average_values``.
        cfg: Current sensor configuration (weights and interval settings).
        energy_average_entity_id_cache: Cache mapping unique_id → entity_id,
            populated during snapshot collection.

    Returns:
        A :class:`ConsumptionPopulation`.  ``ok`` is False when a weight is
        unset or all weights are zero, an average sensor is not registered,
        or more than :data:`MAX_ESTIMATED_LOAD_HOURS` hours have no value —
        the caller should retry on the next cycle **without** flagging
        ``missing_input_entities``.
    """
    w1 = cfg.house_consumption_energy_weight_1d
    w3 = cfg.house_consumption_energy_weight_3d
    w7 = cfg.house_consumption_energy_weight_7d
    w14 = cfg.house_consumption_energy_weight_14d

    for weight, _name in [(w1, "1d"), (w3, "3d"), (w7, "7d"), (w14, "14d")]:
        if weight is None:
            log_planner(
                "warning",
                "[avg] snapshot populator: weight %s is None, returning False",
                _name,
            )
            return ConsumptionPopulation(ok=False)

    scale_to_interval = 60.0 / cfg.recommendation_interval_minutes
    w_total_config = int(w1) + int(w3) + int(w7) + int(w14)
    if w_total_config == 0:
        log_planner("debug", "[avg] all weights sum to 0, returning False")
        return ConsumptionPopulation(ok=False)

    measured: dict[int, _WindowValues] = {}
    missing: list[int] = []

    for h in range(24):
        hour_end = (h + 1) % 24

        uid_1d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 1)
        uid_3d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 3)
        uid_7d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 7)
        uid_14d = get_energy_average_sensor_unique_id(entry_id, h, hour_end, 14)

        log_planner(
            "debug",
            "[avg] snapshot populator: processing hour %d with UIDs "
            "1d=%s, 3d=%s, 7d=%s, 14d=%s",
            h,
            uid_1d,
            uid_3d,
            uid_7d,
            uid_14d,
        )

        eid_1d = energy_average_entity_id_cache.get(uid_1d)
        eid_3d = energy_average_entity_id_cache.get(uid_3d)
        eid_7d = energy_average_entity_id_cache.get(uid_7d)
        eid_14d = energy_average_entity_id_cache.get(uid_14d)

        log_planner(
            "debug",
            "[avg] hour %d: resolved entity IDs from cache "
            "(1d=%s, 3d=%s, 7d=%s, 14d=%s)",
            h,
            eid_1d,
            eid_3d,
            eid_7d,
            eid_14d,
        )

        if eid_1d is None or eid_3d is None or eid_7d is None or eid_14d is None:
            log_planner(
                "debug",
                "[avg] hour %d: missing entity IDs in cache (1d=%s, 3d=%s, 7d=%s, 14d=%s), returning False",
                h,
                eid_1d,
                eid_3d,
                eid_7d,
                eid_14d,
            )
            return ConsumptionPopulation(ok=False)

        v1 = snapshot.energy_average_values.get(eid_1d)
        v3 = snapshot.energy_average_values.get(eid_3d)
        v7 = snapshot.energy_average_values.get(eid_7d)
        v14 = snapshot.energy_average_values.get(eid_14d)

        log_planner(
            "debug",
            "[avg] hour %d: fetched energy average values from snapshot. values are (1d=%s, 3d=%s, 7d=%s, 14d=%s)",
            h,
            v1,
            v3,
            v7,
            v14,
        )

        if v1 is None or v3 is None or v7 is None or v14 is None:
            log_planner(
                "debug",
                "[avg] hour %d: values missing in snapshot for eids "
                "(1d=%s=%s, 3d=%s=%s, 7d=%s=%s, 14d=%s=%s)",
                h,
                eid_1d,
                v1,
                eid_3d,
                v3,
                eid_7d,
                v7,
                eid_14d,
                v14,
            )
            missing.append(h)
            continue

        measured[h] = (v1, v3, v7, v14)

    if len(missing) > MAX_ESTIMATED_LOAD_HOURS:
        log_planner(
            "debug",
            "[avg] %d hour(s) without values (%s) exceed the estimate limit %d, "
            "returning False",
            len(missing),
            missing,
            MAX_ESTIMATED_LOAD_HOURS,
        )
        return ConsumptionPopulation(ok=False, missing_hours=tuple(missing))

    estimates = estimate_missing_hours(measured, missing)
    for h, values in estimates.items():
        log_planner(
            "debug",
            "[avg] hour %d: no stored sample, estimated from neighbours "
            "(1d=%s, 3d=%s, 7d=%s, 14d=%s)",
            h,
            *values,
        )

    for h, (v1, v3, v7, v14) in sorted({**measured, **estimates}.items()):
        avg, _ = weighted_avg_consumption(
            v1,
            v3,
            v7,
            v14,
            int(w1),
            int(w3),
            int(w7),
            int(w14),
        )

        log_planner(
            "debug",
            "[avg] hour %d: calculated weighted average consumption %s kWh (scaled to interval: %s kWh)",
            h,
            round(avg, 3),
            round(avg / scale_to_interval, 3),
        )

        for obj in recommendations:
            if int(obj.start.hour) == int(h):
                obj.avg_house_consumption_kwh = round(avg / scale_to_interval, 3)
                obj.avg_house_consumption_1d_kwh = round(v1 / scale_to_interval, 3)
                obj.avg_house_consumption_3d_kwh = round(v3 / scale_to_interval, 3)
                obj.avg_house_consumption_7d_kwh = round(v7 / scale_to_interval, 3)
                obj.avg_house_consumption_14d_kwh = round(v14 / scale_to_interval, 3)

    log_planner(
        "debug",
        "[avg] snapshot populator: returning True after processing 24 hours "
        "(estimated=%s)",
        missing,
    )
    return ConsumptionPopulation(
        ok=True, missing_hours=tuple(missing), estimated_hours=tuple(missing)
    )


# _compute_weighted_average has been removed. The canonical implementation lives
# in planner.slot_population.weighted_avg_consumption and is imported above.
