"""History reader — queries Home Assistant's recorder for energy sensor data.

Reads historical states from an energy accumulator sensor (e.g. grid import
energy in kWh) and computes per-slot consumption deltas at configurable
resolution (default 15-minute slots = 96 slots/day).

Uses the HA recorder API with proper executor offloading to keep the event
loop responsive.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage] — HA public API, not in stubs
)
from homeassistant.components.recorder.history import (
    get_last_state_changes,
    get_significant_states,
)
from homeassistant.core import HomeAssistant

from custom_components.hsem.ml.slot_deltas import (
    compute_slot_deltas,
    deltas_from_slot_ends,
    slot_ends as completed_slot_ends,
)
from custom_components.hsem.utils.datetime_utils import (
    normalize_datetime,
    now as hsem_now,
    physical_elapsed,
    slot_key,
    utc_key,
)
from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER
from custom_components.hsem.utils.unit_normalize import normalize_to_unit

# ---------------------------------------------------------------------------
# Minimum history required before ML consumption predictions are trusted.
# Batpred uses 1 day; we default to 14 days for a stable weekly pattern.
# ---------------------------------------------------------------------------
DEFAULT_MIN_HISTORY_DAYS = 14

# Maximum history to fetch — protects against excessive DB load for users
# with very long recorder retention.
DEFAULT_MAX_HISTORY_DAYS = 90

# The shortest time a read that found too little history is remembered before
# the recorder is queried again.  History grows by days, so re-checking more
# often only repeats a large query with the same answer (#1302, #1306).  When
# the read knows how much history is missing, the wait stretches to that
# date instead (``HistoryResult.ready_at``, #1355); this value stays the floor
# and is used alone when the recorder held no states at all.
INSUFFICIENT_HISTORY_RETRY = timedelta(minutes=15)

# Default slot resolution in minutes (15-minute slots = 96 per day).
DEFAULT_SLOT_MINUTES = 15


@dataclass(frozen=True, slots=True)
class HistoryResult[SampleT]:
    """Outcome of one recorder history read.

    Attributes:
        samples: The usable history, oldest first.  Empty when the recorder
            held no states, too few states, or less history than required.
        ready_at: When ``samples`` is empty because the recorded span is
            shorter than the required number of days, the physical UTC
            instant at which the span is expected to be long enough (the
            earliest sample plus the required days).  History only grows by
            one day per day, so a caller can skip the recorder until then
            instead of repeating the same large query (#1355).  ``None``
            when the samples are usable or when the recorder held nothing
            to extrapolate from.
        anchor: Energy reads only: the last reading of the newest completed
            slot, as ``(timestamp, accumulator_value)``.  Passing its
            timestamp back as ``since`` continues the history from there
            without re-reading the whole window (#1355).  ``None`` when the
            read produced no completed slot.
    """

    samples: list[SampleT] = field(default_factory=list)
    ready_at: datetime | None = None
    anchor: tuple[datetime, float] | None = None


def insufficient_retry_window(now: datetime, ready_at: datetime | None) -> timedelta:
    """Return how long an insufficient-history outcome should be remembered.

    Args:
        now: When the recorder was read.
        ready_at: :attr:`HistoryResult.ready_at` of that read, or ``None``.

    Returns:
        The time until ``ready_at``, never shorter than
        :data:`INSUFFICIENT_HISTORY_RETRY`, which is also the answer when no
        ready date is known.
    """
    if ready_at is None:
        return INSUFFICIENT_HISTORY_RETRY
    return max(INSUFFICIENT_HISTORY_RETRY, physical_elapsed(ready_at, now))


def _ready_at(earliest: datetime, days: int) -> datetime:
    """Return the UTC instant at which ``earliest`` is ``days`` old."""
    return utc_key(earliest) + timedelta(days=days)


# How many of the newest recorded states are inspected for a unit when the
# live state carries none (an unavailable sensor, or HA still restoring).
_NEWEST_STATES_FOR_UNIT = 20


class HistoryReader:
    """Reads historical energy sensor data from the HA recorder.

    Queries the recorder's ``states`` table for an energy accumulator sensor
    and computes per-slot consumption deltas at configurable resolution.

    Example usage::

        reader = HistoryReader(hass)
        result = await reader.read_energy_history(
            entity_id="sensor.grid_import_energy",
            days=14,
            slot_minutes=15,
        )
        # result.samples is list[tuple[datetime, int, float]]
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the reader.

        Args:
            hass: The Home Assistant instance.
        """
        self._hass = hass

    async def read_energy_history(
        self,
        entity_id: str,
        days: int = DEFAULT_MIN_HISTORY_DAYS,
        max_days: int = DEFAULT_MAX_HISTORY_DAYS,
        slot_minutes: int = DEFAULT_SLOT_MINUTES,
        expected_unit: str | None = None,
        *,
        since: datetime | None = None,
        with_attributes: bool = False,
        source_unit: str | None = None,
    ) -> HistoryResult[tuple[datetime, int, float]]:
        """Read historical energy accumulator data and compute per-slot deltas.

        Args:
            entity_id: The HA entity ID of the energy sensor (e.g.
                ``sensor.grid_import_energy``).  Must be a monotonically
                increasing accumulator (TOTAL_INCREASING state class).
            days: Minimum number of days of history required.
            max_days: Maximum days of history to fetch (performance guard).
            slot_minutes: Slot width in minutes (default 15).  Must divide
                evenly into 60.  Supported: 15, 30, 60.
            expected_unit: HSEM's canonical unit for this field (e.g.
                ``UnitOfEnergy.KILO_WATT_HOUR``). When given, each state's
                declared ``unit_of_measurement`` is compared against it and
                the raw accumulator reading is converted via
                :func:`custom_components.hsem.utils.unit_normalize.normalize_to_unit`
                *before* delta computation, so a Wh-reporting (or unit-less
                template) energy meter cannot silently corrupt the computed
                deltas (issue #946). ``None`` (the default) skips
                normalization entirely.
            since: Timestamp of a previously returned ``anchor``.  When
                given, only states from one slot before it are read and only
                slots after the anchor's slot are returned, so a caller that
                already holds the older history pays for the new rows alone
                (#1355).  The minimum-days check is skipped: the caller
                knows the span it already holds.
            with_attributes: Read every state's own ``unit_of_measurement``
                (the attributes join, one extra row per state).  Needed only
                while the window may contain a unit change; otherwise the
                recorder is queried with ``no_attributes=True`` and
                ``source_unit`` applies to every state.
            source_unit: The unit every state is assumed to report when
                ``with_attributes`` is off, normally from
                :meth:`resolve_unit`.  Ignored when ``expected_unit`` is
                ``None``.

        Returns:
            A :class:`HistoryResult` whose samples are
            ``(datetime, slot_index, energy_kwh)`` tuples sorted from oldest
            to newest.  The samples are empty if insufficient history is
            available; ``ready_at`` then says when enough is expected.

            ``slot_index`` is 0-based within the day (0 = 00:00-00:15 for
            15-min slots, up to 95 = 23:45-00:00).
        """
        now = hsem_now()
        end_time = utc_key(now)
        if since is None:
            start_time = end_time - timedelta(days=max_days)
        else:
            # One slot earlier so the anchor's own slot is complete in the
            # result; its delta is dropped again below as the caller has it.
            start_time = utc_key(since) - timedelta(minutes=slot_minutes)

        _LOGGER.debug(
            "ML history: fetching states for %s from %s to %s (%s)",
            entity_id,
            start_time.isoformat(),
            now.isoformat(),
            "incremental" if since is not None else "full",
        )

        # The recorder query, the per-state conversion and the delta pass all
        # run in the recorder executor: a 90-day window holds hundreds of
        # thousands of states, too many to iterate on the event loop (#1302).
        return await get_instance(self._hass).async_add_executor_job(
            self._energy_history_blocking,
            entity_id,
            start_time,
            now,
            days,
            slot_minutes,
            expected_unit,
            since,
            with_attributes,
            source_unit,
        )

    def _energy_history_blocking(
        self,
        entity_id: str,
        start_time: datetime,
        now: datetime,
        days: int,
        slot_minutes: int,
        expected_unit: str | None,
        since: datetime | None,
        with_attributes: bool,
        source_unit: str | None,
    ) -> HistoryResult[tuple[datetime, int, float]]:
        """Run :meth:`read_energy_history`'s blocking part in the executor."""
        readings = self._query_readings(
            entity_id,
            start_time,
            utc_key(now),
            lambda state_obj: state_obj.state,
            expected_unit,
            "energy history",
            with_attributes=with_attributes,
            source_unit=source_unit,
        )
        if readings is None:
            _LOGGER.warning(
                "ML history: no recorder states found for %s. "
                "Is the recorder storing this entity? "
                "Check exclude settings and purge_keep_days.",
                entity_id,
            )
            return HistoryResult()

        if len(readings) < 2:
            if since is not None:
                _LOGGER.debug(
                    "ML history: no new readings for %s since %s.",
                    entity_id,
                    since.isoformat(),
                )
                return HistoryResult()
            _LOGGER.warning(
                "ML history: need at least 2 readings"
                " (got %d) to compute deltas for %s",
                len(readings),
                entity_id,
            )
            return HistoryResult()

        # Compute per-slot consumption deltas from the accumulator.
        slot_ends = completed_slot_ends(readings, now, slot_minutes)
        history = deltas_from_slot_ends(slot_ends, slot_minutes)
        anchor = (slot_ends[-1][1], slot_ends[-1][2]) if slot_ends else None

        if since is not None:
            # The caller holds every slot up to and including the anchor's.
            since_key = slot_key(since, slot_minutes)
            history = [sample for sample in history if utc_key(sample[0]) > since_key]
            return HistoryResult(history, anchor=anchor)

        # Check minimum history requirement.
        if history:
            earliest = history[0][0]
            actual_days = (utc_key(now) - utc_key(earliest)).total_seconds() / 86400.0
            if actual_days < days:
                ready_at = _ready_at(earliest, days)
                _LOGGER.info(
                    "ML history: only %.1f days available for %s (need %d); "
                    "enough history is expected at %s. "
                    "Predictions will use fallback.",
                    actual_days,
                    entity_id,
                    days,
                    normalize_datetime(ready_at).isoformat(),
                )
                return HistoryResult(ready_at=ready_at)

        return HistoryResult(history, anchor=anchor)

    async def resolve_unit(self, entity_id: str) -> str | None:
        """Return the unit *entity_id* reports in, for attribute-free reads.

        The live state's ``unit_of_measurement`` is used when present.  An
        unavailable sensor, or one HA is still restoring, carries none; the
        newest recorded states are then inspected so a Wh meter is not
        mistaken for a kWh one for a whole window (#1355).

        Args:
            entity_id: The HA entity ID.

        Returns:
            The unit, or ``None`` when neither the live state nor the
            recorder declares one (a unit-less template sensor).
        """
        state = self._hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        if unit:
            return str(unit)
        return await get_instance(self._hass).async_add_executor_job(
            self._newest_recorded_unit, entity_id
        )

    def _newest_recorded_unit(self, entity_id: str) -> str | None:
        """Return the unit of the newest recorded state that declares one."""
        states = get_last_state_changes(
            self._hass, _NEWEST_STATES_FOR_UNIT, entity_id
        ).get(entity_id, [])
        for state_obj in reversed(states):
            unit = state_obj.attributes.get("unit_of_measurement")
            if unit:
                return str(unit)
        return None

    async def read_instantaneous_history(
        self,
        entity_id: str,
        days: int = DEFAULT_MIN_HISTORY_DAYS,
        max_days: int = DEFAULT_MAX_HISTORY_DAYS,
        expected_unit: str | None = None,
        *,
        with_attributes: bool = False,
        source_unit: str | None = None,
    ) -> HistoryResult[tuple[datetime, float]]:
        """Read historical instantaneous sensor values (e.g. temperature).

        Unlike :meth:`read_energy_history`, this does NOT compute deltas —
        it returns raw (timestamp, value) pairs from the recorder.

        Args:
            entity_id: The HA entity ID (e.g. ``sensor.outdoor_temperature``).
            days: Minimum days of history required.
            max_days: Maximum days to fetch.
            expected_unit: HSEM's canonical unit for this field (e.g.
                ``UnitOfTemperature.CELSIUS``). When given, each state's
                declared ``unit_of_measurement`` is compared against it and
                the value is converted via
                :func:`custom_components.hsem.utils.unit_normalize.normalize_to_unit`
                so a °F-reporting or unit-less template sensor cannot
                silently corrupt the reading (issue #945). ``None`` (the
                default) skips normalization entirely.
            with_attributes: Read every state's own unit instead of
                assuming ``source_unit``; see :meth:`read_energy_history`.
            source_unit: The unit every state is assumed to report when
                ``with_attributes`` is off, normally from
                :meth:`resolve_unit`.

        Returns:
            A :class:`HistoryResult` of ``(timestamp, value)`` samples sorted
            oldest-first.  The samples are empty if insufficient history is
            available; ``ready_at`` then says when enough is expected.
        """
        return await self._read_instantaneous(
            entity_id,
            days,
            max_days,
            lambda state_obj: state_obj.state,
            expected_unit=expected_unit,
            with_attributes=with_attributes,
            source_unit=source_unit,
        )

    async def read_instantaneous_attribute_history(
        self,
        entity_id: str,
        attribute: str,
        days: int = DEFAULT_MIN_HISTORY_DAYS,
        max_days: int = DEFAULT_MAX_HISTORY_DAYS,
    ) -> HistoryResult[tuple[datetime, float]]:
        """Read historical values of one *attribute* of an entity's state.

        Unlike :meth:`read_instantaneous_history`, this reads a named
        attribute (e.g. ``wind_speed`` on a ``weather`` entity) rather than
        the entity's own state value — useful for deriving a history series
        from an entity that doesn't expose the value as its primary state.

        Args:
            entity_id: The HA entity ID (e.g. ``weather.home``).
            attribute: The attribute name to read off each recorded state.
            days: Minimum days of history required.
            max_days: Maximum days to fetch.

        Returns:
            A :class:`HistoryResult` of ``(timestamp, value)`` samples sorted
            oldest-first.  The samples are empty if insufficient history is
            available; ``ready_at`` then says when enough is expected.
        """
        return await self._read_instantaneous(
            entity_id,
            days,
            max_days,
            lambda state_obj: state_obj.attributes.get(attribute),
            with_attributes=True,
        )

    async def _read_instantaneous(
        self,
        entity_id: str,
        days: int,
        max_days: int,
        value_getter: Callable[[Any], Any],
        expected_unit: str | None = None,
        *,
        with_attributes: bool,
        source_unit: str | None = None,
    ) -> HistoryResult[tuple[datetime, float]]:
        """Shared recorder query + extraction for instantaneous value history.

        Args:
            entity_id: The HA entity ID to query.
            days: Minimum days of history required.
            max_days: Maximum days to fetch.
            value_getter: Extracts the raw value from each recorder ``State``
                (e.g. ``lambda s: s.state`` or ``lambda s: s.attributes.get(...)``).
            expected_unit: HSEM's canonical unit for this field. When given,
                each state's declared ``unit_of_measurement`` is compared
                against it and the value is converted via
                :func:`custom_components.hsem.utils.unit_normalize.normalize_to_unit`
                (issue #945). ``None`` (the default) skips normalization.
            with_attributes: Whether the recorder query joins the state
                attributes; ``value_getter`` may only read them when it does.
            source_unit: The unit assumed for every state when
                ``with_attributes`` is off.

        Returns:
            A :class:`HistoryResult` of ``(timestamp, value)`` samples sorted
            oldest-first.  The samples are empty if insufficient history is
            available; ``ready_at`` then says when enough is expected.
        """
        now = hsem_now()
        end_time = utc_key(now)
        start_time = end_time - timedelta(days=max_days)

        return await get_instance(self._hass).async_add_executor_job(
            self._instantaneous_blocking,
            entity_id,
            start_time,
            now,
            days,
            value_getter,
            expected_unit,
            with_attributes,
            source_unit,
        )

    def _instantaneous_blocking(
        self,
        entity_id: str,
        start_time: datetime,
        now: datetime,
        days: int,
        value_getter: Callable[[Any], Any],
        expected_unit: str | None,
        with_attributes: bool,
        source_unit: str | None,
    ) -> HistoryResult[tuple[datetime, float]]:
        """Run :meth:`_read_instantaneous`'s blocking part in the executor."""
        readings = self._query_readings(
            entity_id,
            start_time,
            utc_key(now),
            value_getter,
            expected_unit,
            "instantaneous history",
            with_attributes=with_attributes,
            source_unit=source_unit,
        )
        if readings is None:
            _LOGGER.warning(
                "ML history: no instantaneous states found for %s.", entity_id
            )
            return HistoryResult()

        if readings:
            earliest = readings[0][0]
            actual_days = (utc_key(now) - utc_key(earliest)).total_seconds() / 86400.0
            if actual_days < days:
                ready_at = _ready_at(earliest, days)
                _LOGGER.info(
                    "ML history: only %.1f days available for %s (need %d); "
                    "enough history is expected at %s.",
                    actual_days,
                    entity_id,
                    days,
                    normalize_datetime(ready_at).isoformat(),
                )
                return HistoryResult(ready_at=ready_at)

        return HistoryResult(readings)

    async def read_today_actuals(
        self,
        entity_id: str,
        slot_minutes: int = DEFAULT_SLOT_MINUTES,
        expected_unit: str | None = None,
    ) -> dict[datetime, float]:
        """Read today's completed-slot actual consumption from the energy sensor.

        Queries from one physical slot before HA-local midnight through now,
        then computes deltas for today's completed slots. In-progress and
        future slots are excluded.

        Args:
            entity_id: The energy accumulator entity ID.
            slot_minutes: Slot width in minutes.
            expected_unit: HSEM's canonical unit for this field (e.g.
                ``UnitOfEnergy.KILO_WATT_HOUR``). When given, each state's
                declared ``unit_of_measurement`` is compared against it and
                the raw accumulator reading is normalized before delta
                computation (issue #946). ``None`` (the default) skips
                normalization entirely.

        Returns:
            Dict mapping canonical UTC slot-start datetime → ``actual_kwh``
            for completed slots only.  Physical datetime identity is required
            so both folds of an autumn repeated hour remain distinct.  Empty
            dict if no data is available.
        """
        now = hsem_now()
        midnight = now.replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
            fold=0,
        )
        # Include one physical slot before local midnight.  Its final reading
        # is the left boundary needed to calculate the first slot of today.
        start_time = utc_key(midnight) - timedelta(minutes=slot_minutes)

        return await get_instance(self._hass).async_add_executor_job(
            self._today_actuals_blocking,
            entity_id,
            start_time,
            now,
            slot_minutes,
            expected_unit,
        )

    def _today_actuals_blocking(
        self,
        entity_id: str,
        start_time: datetime,
        now: datetime,
        slot_minutes: int,
        expected_unit: str | None,
    ) -> dict[datetime, float]:
        """Run :meth:`read_today_actuals`'s blocking part in the executor."""
        # Fetch the DST-safe local-day boundary range.
        readings = self._query_readings(
            entity_id,
            start_time,
            utc_key(now),
            lambda state_obj: state_obj.state,
            expected_unit,
            "today actuals",
            with_attributes=True,
        )
        if readings is None or len(readings) < 2:
            return {}

        # Compute per-slot deltas for today only.
        history = compute_slot_deltas(readings, now, slot_minutes)

        # Filter to today's completed slots and key by physical slot identity.
        # ``compute_slot_deltas`` has already excluded the in-progress slot.
        actuals: dict[datetime, float] = {}
        for slot_start, _slot_idx, energy in history:
            if slot_start.date() != now.date():
                continue
            actuals[slot_key(slot_start, slot_minutes)] = energy

        return actuals

    def _query_readings(
        self,
        entity_id: str,
        start_time: datetime,
        end_time: datetime,
        value_getter: Callable[[Any], Any],
        expected_unit: str | None,
        label: str,
        *,
        with_attributes: bool,
        source_unit: str | None = None,
    ) -> list[tuple[datetime, float]] | None:
        """Query the recorder and convert states to sorted readings.

        Blocking: call from the recorder executor only.

        Args:
            entity_id: The HA entity ID to query.
            start_time: Start of the query window (UTC).
            end_time: End of the query window (UTC).
            value_getter: Extracts the raw value from each recorder ``State``.
            expected_unit: HSEM's canonical unit for this field, or ``None``
                to skip unit normalization (issues #945, #946).
            label: Short description of the read, used in unit warnings.
            with_attributes: Join the state attributes.  Off, the query skips
                one ``state_attributes`` row per state (a utility meter
                writes a distinct one for every update, #1355) and
                ``source_unit`` stands in for every state's unit.
            source_unit: The unit assumed for every state when
                ``with_attributes`` is off.

        Returns:
            ``(timestamp, value)`` pairs sorted oldest-first, skipping states
            that are non-numeric, non-finite or in an unconvertible unit.
            ``None`` when the recorder holds no states for the entity.
        """
        # Every state change of an accumulator counts, so
        # significant_changes_only is off.
        states = get_significant_states(
            self._hass,
            start_time,
            end_time,
            [entity_id],
            include_start_time_state=True,
            significant_changes_only=False,
            no_attributes=not with_attributes,
        )

        # Default arguments yield full ``State`` objects, never the
        # compressed dict form, so attribute access below is safe.
        entity_states: list[Any] = states.get(entity_id, [])
        if not entity_states:
            return None

        _LOGGER.debug(
            "ML history: got %d raw states for %s",
            len(entity_states),
            entity_id,
        )

        readings: list[tuple[datetime, float]] = []
        for state_obj in entity_states:
            try:
                ts = normalize_datetime(state_obj.last_updated)
                value = float(value_getter(state_obj))
                if not math.isfinite(value):
                    continue
                if expected_unit is not None:
                    unit = (
                        state_obj.attributes.get("unit_of_measurement")
                        if with_attributes
                        else source_unit
                    )
                    normalized = normalize_to_unit(
                        value,
                        unit,
                        expected_unit,
                        entity_id=entity_id,
                        label=label,
                    )
                    if normalized is None or not math.isfinite(normalized):
                        continue
                    value = normalized
                readings.append((ts, value))
            except ValueError, TypeError, AttributeError:
                continue

        readings.sort(key=lambda item: utc_key(item[0]))
        return readings
