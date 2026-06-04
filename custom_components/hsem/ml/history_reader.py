"""History reader — queries Home Assistant's recorder for energy sensor data.

Reads historical states from an energy accumulator sensor (e.g. grid import
energy in kWh) and computes per-slot consumption deltas at configurable
resolution (default 15-minute slots = 96 slots/day).

Uses the HA recorder API with proper executor offloading to keep the event
loop responsive.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.recorder import (
    get_instance,  # pyright: ignore[reportPrivateImportUsage] — HA public API, not in stubs
)
from homeassistant.components.recorder.history import get_significant_states
from homeassistant.core import HomeAssistant

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

# ---------------------------------------------------------------------------
# Minimum history required before ML consumption predictions are trusted.
# Batpred uses 1 day; we default to 14 days for a stable weekly pattern.
# ---------------------------------------------------------------------------
DEFAULT_MIN_HISTORY_DAYS = 14

# Maximum history to fetch — protects against excessive DB load for users
# with very long recorder retention.
DEFAULT_MAX_HISTORY_DAYS = 90

# Default slot resolution in minutes (15-minute slots = 96 per day).
DEFAULT_SLOT_MINUTES = 15

# Maximum sane per-slot consumption in kWh (cap for data errors).
MAX_SLOT_KWH = 12.5


class HistoryReader:
    """Reads historical energy sensor data from the HA recorder.

    Queries the recorder's ``states`` table for an energy accumulator sensor
    and computes per-slot consumption deltas at configurable resolution.

    Example usage::

        reader = HistoryReader(hass)
        history = await reader.read_energy_history(
            entity_id="sensor.grid_import_energy",
            days=14,
            slot_minutes=15,
        )
        # history is list[tuple[datetime, int, float]]
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
    ) -> list[tuple[datetime, int, float]]:
        """Read historical energy accumulator data and compute per-slot deltas.

        Args:
            entity_id: The HA entity ID of the energy sensor (e.g.
                ``sensor.grid_import_energy``).  Must be a monotonically
                increasing accumulator (TOTAL_INCREASING state class).
            days: Minimum number of days of history required.
            max_days: Maximum days of history to fetch (performance guard).
            slot_minutes: Slot width in minutes (default 15).  Must divide
                evenly into 60.  Supported: 15, 30, 60.

        Returns:
            A list of ``(datetime, slot_index, energy_kwh)`` tuples sorted
            from oldest to newest.  Returns an empty list if insufficient
            history is available.

            ``slot_index`` is 0-based within the day (0 = 00:00-00:15 for
            15-min slots, up to 95 = 23:45-00:00).
        """
        now = datetime.now().astimezone()
        start_time = now - timedelta(days=max_days)

        _LOGGER.debug(
            "ML history: fetching states for %s from %s to %s",
            entity_id,
            start_time.isoformat(),
            now.isoformat(),
        )

        # Offload the blocking recorder call to the executor.
        states: dict[str, list[Any]] = await get_instance(
            self._hass
        ).async_add_executor_job(
            get_significant_states,
            self._hass,
            start_time,
            now,
            [entity_id],
            None,  # no significant_changes_only filter
            True,  # minimal_response
            False,  # no_attributes
        )

        entity_states = states.get(entity_id, [])
        if not entity_states:
            _LOGGER.warning(
                "ML history: no recorder states found for %s. "
                "Is the recorder storing this entity? "
                "Check exclude settings and purge_keep_days.",
                entity_id,
            )
            return []

        _LOGGER.debug(
            "ML history: got %d raw states for %s",
            len(entity_states),
            entity_id,
        )

        # Convert state objects to (datetime, float_value) pairs.
        readings: list[tuple[datetime, float]] = []
        for state_obj in entity_states:
            try:
                ts = state_obj.last_updated
                value = float(state_obj.state)
                readings.append((ts, value))
            except (ValueError, TypeError, AttributeError):
                continue

        if len(readings) < 2:
            _LOGGER.warning(
                "ML history: need at least 2 readings"
                " (got %d) to compute deltas for %s",
                len(readings),
                entity_id,
            )
            return []

        # Sort by timestamp ascending.
        readings.sort(key=lambda x: x[0])

        # Compute per-slot consumption deltas from the accumulator.
        slots_per_day = 24 * 60 // slot_minutes
        history = self._compute_slot_deltas(readings, now, slot_minutes, slots_per_day)

        # Check minimum history requirement.
        if history:
            earliest = history[0][0]
            actual_days = (now - earliest).total_seconds() / 86400.0
            if actual_days < days:
                _LOGGER.info(
                    "ML history: only %.1f days available for %s (need %d). "
                    "Predictions will use fallback.",
                    actual_days,
                    entity_id,
                    days,
                )
                return []

        return history

    async def read_instantaneous_history(
        self,
        entity_id: str,
        days: int = DEFAULT_MIN_HISTORY_DAYS,
        max_days: int = DEFAULT_MAX_HISTORY_DAYS,
    ) -> list[tuple[datetime, float]]:
        """Read historical instantaneous sensor values (e.g. temperature).

        Unlike :meth:`read_energy_history`, this does NOT compute deltas —
        it returns raw (timestamp, value) pairs from the recorder.

        Args:
            entity_id: The HA entity ID (e.g. ``sensor.outdoor_temperature``).
            days: Minimum days of history required.
            max_days: Maximum days to fetch.

        Returns:
            List of ``(timestamp, value)`` sorted oldest-first.
            Empty list if insufficient history.
        """
        now = datetime.now().astimezone()
        start_time = now - timedelta(days=max_days)

        states: dict[str, list[Any]] = await get_instance(
            self._hass
        ).async_add_executor_job(
            get_significant_states,
            self._hass,
            start_time,
            now,
            [entity_id],
            None,
            True,
            False,
        )

        entity_states = states.get(entity_id, [])
        if not entity_states:
            _LOGGER.warning(
                "ML history: no instantaneous states found for %s.", entity_id
            )
            return []

        readings: list[tuple[datetime, float]] = []
        for state_obj in entity_states:
            try:
                ts = state_obj.last_updated
                value = float(state_obj.state)
                readings.append((ts, value))
            except (ValueError, TypeError, AttributeError):
                continue

        readings.sort(key=lambda x: x[0])

        if readings:
            earliest = readings[0][0]
            actual_days = (now - earliest).total_seconds() / 86400.0
            if actual_days < days:
                _LOGGER.info(
                    "ML history: only %.1f days available for %s (need %d).",
                    actual_days,
                    entity_id,
                    days,
                )
                return []

        return readings

    async def read_today_actuals(
        self,
        entity_id: str,
        slot_minutes: int = DEFAULT_SLOT_MINUTES,
    ) -> dict[int, float]:
        """Read today's completed-slot actual consumption from the energy sensor.

        Queries the recorder for the last 24 hours of the energy accumulator
        and computes per-slot deltas for today's slots that have already
        ended.  In-progress and future slots are excluded.

        Args:
            entity_id: The energy accumulator entity ID.
            slot_minutes: Slot width in minutes.

        Returns:
            Dict mapping ``slot_index`` → ``actual_kwh`` for completed
            slots only.  Empty dict if no data is available.
        """
        now = datetime.now().astimezone()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

        # Fetch last 24h of states.
        states: dict[str, list[Any]] = await get_instance(
            self._hass
        ).async_add_executor_job(
            get_significant_states,
            self._hass,
            midnight - (midnight.utcoffset() or timedelta(0)),
            now,
            [entity_id],
            None,
            True,
            False,
        )

        entity_states = states.get(entity_id, [])
        if not entity_states:
            return {}

        # Convert to (timestamp, value) pairs.
        readings: list[tuple[datetime, float]] = []
        for state_obj in entity_states:
            try:
                ts = state_obj.last_updated
                value = float(state_obj.state)
                readings.append((ts, value))
            except (ValueError, TypeError, AttributeError):
                continue

        if len(readings) < 2:
            return {}

        readings.sort(key=lambda x: x[0])

        # Compute per-slot deltas for today only.
        slots_per_day = 24 * 60 // slot_minutes
        history = self._compute_slot_deltas(readings, now, slot_minutes, slots_per_day)

        # Filter to today's completed slots and index by slot number.
        actuals: dict[int, float] = {}
        for _ts, slot_idx, energy in history:
            actuals[slot_idx] = energy

        return actuals

    @staticmethod
    def _compute_slot_deltas(
        readings: list[tuple[datetime, float]],
        now: datetime,
        slot_minutes: int,
        slots_per_day: int,
    ) -> list[tuple[datetime, int, float]]:
        """Compute per-slot energy deltas from accumulator readings.

        Groups readings into time slots of ``slot_minutes`` width and computes
        the delta between the last reading of consecutive slots.

        Args:
            readings: Sorted list of ``(timestamp, accumulator_value)``.
            now: Current time (used to skip incomplete slots).
            slot_minutes: Width of each slot in minutes.
            slots_per_day: Total slots per 24-hour day.

        Returns:
            List of ``(slot_start_dt, slot_index, energy_kwh)``.
        """

        # Compute a unique slot key for each reading:
        #   day_number * slots_per_day + slot_index
        def slot_key(ts: datetime) -> int:
            minutes_since_midnight = ts.hour * 60 + ts.minute
            slot_idx = minutes_since_midnight // slot_minutes
            return ts.toordinal() * slots_per_day + slot_idx

        # Group readings by slot key.
        slot_groups: dict[int, list[tuple[datetime, float]]] = {}
        for ts, val in readings:
            key = slot_key(ts)
            slot_groups.setdefault(key, []).append((ts, val))

        # Get the last reading of each slot.
        slot_ends: list[tuple[int, datetime, float]] = []
        for key in sorted(slot_groups.keys()):
            group = slot_groups[key]
            last_ts, last_val = max(group, key=lambda x: x[0])
            slot_ends.append((key, last_ts, last_val))

        # Compute the slot key for "now" and skip incomplete current slot.
        now_key = slot_key(now)
        slot_ends = [(k, ts, v) for k, ts, v in slot_ends if k < now_key]

        if len(slot_ends) < 2:
            return []

        # Compute deltas between consecutive slots.
        history: list[tuple[datetime, int, float]] = []
        for i in range(1, len(slot_ends)):
            _prev_key, prev_ts, prev_val = slot_ends[i - 1]
            curr_key, curr_ts, curr_val = slot_ends[i]
            delta_kwh = curr_val - prev_val

            # Skip negative deltas (accumulator resets) and zero deltas.
            if delta_kwh <= 0:
                continue

            # Cap unreasonably large deltas.
            if delta_kwh > MAX_SLOT_KWH:
                continue

            # Compute slot start time and slot index within the day.
            slot_start = prev_ts.replace(
                minute=(prev_ts.minute // slot_minutes) * slot_minutes,
                second=0,
                microsecond=0,
            )
            slot_index = (slot_start.hour * 60 + slot_start.minute) // slot_minutes

            history.append((slot_start, slot_index, round(delta_kwh, 4)))

        return history
