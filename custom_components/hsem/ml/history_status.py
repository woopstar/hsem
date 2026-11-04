"""Why the ML consumption forecast is, or is not, in use (issue #1365).

The populator returns ``(False, None)`` whenever it falls back to the legacy
average sensors, which covers everything from "the ML switch is off" to "the
recorder holds 38.6 of the 40 required days".  Until #1365 the Plan Strategy
sensor could only publish ``ml_available_history_days: 0.0`` for every one of
those cases, which reads as "no history at all" while the site is merely a
day short.  :class:`MLHistoryStatus` carries the distinction so the sensor
and the per-cycle log line can say what is actually going on.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.hsem.models.sensor_config import SensorConfig

#: The effective configuration a populate ran under: ``id(hass)``, energy
#: entity, export entity (net mode only), net mode, slot minutes, minimum days.
type HistoryCacheKey = tuple[int, str, str | None, bool, int, int]


class MLHistoryState(StrEnum):
    """Outcome of the most recent ML consumption populate."""

    #: No populate has run yet in this process.
    PENDING = "pending"
    #: ``ml_consumption_enabled`` is off; the legacy averages are in use.
    DISABLED = "disabled"
    #: The recorder holds history, but less than the configured minimum.
    INSUFFICIENT_HISTORY = "insufficient_history"
    #: The recorder returned nothing usable for the energy entity.
    RECORDER_EMPTY = "recorder_empty"
    #: The predictor is trained and its predictions populate the slots.
    READY = "ready"
    #: The populate failed for a reason other than history length.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class MLHistoryStatus:
    """What the ML populator concluded, for diagnostics.

    Attributes:
        state: The outcome category.
        available_history_days: Days of recorder history measured by the
            populate — the real span, also when it fell short of the minimum.
            ``None`` when nothing was measured (disabled, pending, failed
            before reading the recorder).
        ready_at: When enough history is expected to exist, for
            :attr:`MLHistoryState.INSUFFICIENT_HISTORY`; ``None`` otherwise.
        reason: Free-text detail for the log line (which entity fell short,
            why the populate failed).
    """

    state: MLHistoryState
    available_history_days: float | None = None
    ready_at: datetime | None = None
    reason: str | None = None

    @property
    def is_waiting_for_history(self) -> bool:
        """Return whether the fallback is a known wait rather than a failure."""
        return self.state in (
            MLHistoryState.INSUFFICIENT_HISTORY,
            MLHistoryState.RECORDER_EMPTY,
        )

    def as_attributes(self) -> dict[str, Any]:
        """Return the sensor attributes that describe this status."""
        return {
            "ml_status": self.state.value,
            "ml_available_history_days": (
                None
                if self.available_history_days is None
                else round(self.available_history_days, 1)
            ),
            "ml_history_ready_at": (
                None if self.ready_at is None else self.ready_at.isoformat()
            ),
        }

    def describe(self, min_days: int) -> str:
        """Return a one-line summary for the per-cycle log."""
        parts = [self.state.value]
        if self.available_history_days is not None:
            parts.append(f"{self.available_history_days:.1f}/{min_days} days")
        if self.ready_at is not None:
            parts.append(f"next check {self.ready_at.isoformat()}")
        if self.reason:
            parts.append(self.reason)
        return ", ".join(parts)


def available_days_from_ready_at(
    now: datetime, ready_at: datetime, min_days: int
) -> float:
    """Return the recorded span implied by an expected ready instant.

    The history reader reports when the span will reach ``min_days``
    (``earliest sample + min_days``); the span it measured is therefore
    ``min_days`` minus the time still to wait.

    Args:
        now: When the recorder was read.
        ready_at: :attr:`~custom_components.hsem.ml.history_reader.HistoryResult.ready_at`.
        min_days: The configured minimum history in days.

    Returns:
        The measured span in days, never negative.
    """
    remaining_days = (ready_at - now).total_seconds() / 86400.0
    return max(min_days - remaining_days, 0.0)


def insufficient_history_status(
    now: datetime, ready_at: datetime | None, min_days: int, reason: str
) -> MLHistoryStatus:
    """Return the status for a recorder read that produced no usable samples.

    Args:
        now: When the recorder was read.
        ready_at: When enough history is expected, or ``None`` when the
            recorder held nothing to extrapolate from.
        min_days: The configured minimum history in days.
        reason: Which read fell short, for the log line.

    Returns:
        :attr:`MLHistoryState.INSUFFICIENT_HISTORY` with the measured span
        and ``ready_at`` when a ready date is known, otherwise
        :attr:`MLHistoryState.RECORDER_EMPTY` with a span of ``0.0``.
    """
    if ready_at is None:
        return MLHistoryStatus(
            MLHistoryState.RECORDER_EMPTY,
            available_history_days=0.0,
            reason=reason,
        )
    return MLHistoryStatus(
        MLHistoryState.INSUFFICIENT_HISTORY,
        available_history_days=available_days_from_ready_at(now, ready_at, min_days),
        ready_at=ready_at,
        reason=reason,
    )


# The outcome of the latest populate per effective configuration, shared by
# the populator (writer) and the coordinator / Plan Strategy sensor (readers).
_status_by_key: dict[HistoryCacheKey, MLHistoryStatus] = {}


def history_cache_key(hass: HomeAssistant, cfg: SensorConfig) -> HistoryCacheKey | None:
    """Return the effective-configuration key, or ``None`` without an entity.

    Args:
        hass: The Home Assistant instance.
        cfg: The sensor configuration.

    Returns:
        The key the populator caches under, or ``None`` when neither the ML
        energy entity nor the grid import energy entity is configured.
    """
    energy_entity = cfg.ml_consumption_energy_entity or cfg.grid_import_energy_entity
    if not energy_entity:
        return None
    net_enabled = cfg.ml_consumption_net_consumption
    return (
        id(hass),
        energy_entity,
        cfg.grid_export_energy_entity if net_enabled else None,
        net_enabled,
        cfg.recommendation_interval_minutes,
        cfg.ml_consumption_history_days,
    )


def record_history_status(key: HistoryCacheKey, status: MLHistoryStatus) -> None:
    """Remember the outcome of a populate under its configuration key."""
    _status_by_key[key] = status


def last_history_status(hass: HomeAssistant, cfg: SensorConfig) -> MLHistoryStatus:
    """Return what the latest populate for this configuration concluded.

    Args:
        hass: The Home Assistant instance the populate ran under.
        cfg: The sensor configuration the populate used.

    Returns:
        The recorded status; :attr:`MLHistoryState.FAILED` when no energy
        entity is configured; :attr:`MLHistoryState.PENDING` when no populate
        has run for this configuration yet.
    """
    key = history_cache_key(hass, cfg)
    if key is None:
        return MLHistoryStatus(MLHistoryState.FAILED, reason="no energy entity")
    return _status_by_key.get(key, MLHistoryStatus(MLHistoryState.PENDING))
