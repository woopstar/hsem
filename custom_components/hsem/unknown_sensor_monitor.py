"""Log why an HSEM sensor is unknown, once per reason change (issue #1482).

The monitor is called once per coordinator cycle.  For every sensor entity of
the coordinator's platform it reads the state Home Assistant holds and, for
an ``unknown`` one, the reason the entity gives through its
``unknown_reason`` property.  It writes one line when a sensor's reason
changes and one when the sensor has a value again, so an unchanged reason is
never repeated.

A sensor that is ``unknown`` and gives no reason is reported with one
warning: every unknown HSEM sensor is meant to say why.  Nothing is logged
while Home Assistant starts, when source entities are not ready and most
sensors have no value for a few cycles.

No sensor is on an allow-list, because none is unknown by design without a
reason.  Before the first cycle the other sensors are ``unavailable`` or show
their restored state.  The EV and OCPP sensors publish a state of their own
without a car or a session.  The hourly house consumption sensors and the
derived house load, ``unavailable`` by design, are not watched.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity

from custom_components.hsem.utils.datetime_utils import now as hsem_now
from custom_components.hsem.utils.logger import async_log
from custom_components.hsem.utils.unknown_reason import LOG_LEVELS, classify

#: Checks after a start in which nothing is logged: the monitor reports from
#: the third coordinator cycle on.
STARTUP_CHECKS = 3

#: Seconds after the first check in which nothing is logged.  State changes
#: can run the first cycles back to back, before the trackers have loaded
#: their history and the first performance evaluation has finished.
STARTUP_SECONDS = 300.0

#: Checks in a row a sensor must be unknown without a reason before the
#: warning.  One cycle without a reason is a state that is about to be
#: written, not a sensor that gives none.
UNEXPLAINED_CHECKS = 3

#: Marks a sensor whose missing reason has been reported.
_UNEXPLAINED = ""


class UnknownSensorMonitor:
    """Reports each watched sensor's unknown reason when it changes."""

    def __init__(self) -> None:
        """Initialise a monitor that watches no entity yet."""
        self._entities: list[Entity] = []
        self._checks = 0
        self._first_check: datetime | None = None
        self._reported: dict[str, str] = {}
        self._unexplained: dict[str, int] = {}

    def watch(self, entities: Iterable[Entity]) -> None:
        """Add sensor entities to watch.

        Args:
            entities: Entities of the sensor platform.  One that gives the
                reason for an unknown value has an ``unknown_reason``
                property returning the code, or ``None``.
        """
        self._entities.extend(entities)

    def check(self, hass: HomeAssistant, now: datetime | None = None) -> None:
        """Log every reason that changed since the last check.

        A failure is logged at debug level and never reaches the caller: the
        monitor only reports, and must not fail a coordinator cycle.

        Args:
            hass: Home Assistant instance.
            now: Current time (timezone-aware).  Defaults to the clock.
        """
        now = now or hsem_now()
        if self._first_check is None:
            self._first_check = now
        self._checks += 1
        if (
            self._checks < STARTUP_CHECKS
            or (now - self._first_check).total_seconds() < STARTUP_SECONDS
        ):
            return
        for entity in self._entities:
            try:
                self._check_entity(hass, entity)
            except Exception as err:
                async_log(
                    "debug",
                    "Unknown-sensor check of %s failed: %s",
                    getattr(entity, "entity_id", None),
                    err,
                )

    def _check_entity(self, hass: HomeAssistant, entity: Entity) -> None:
        """Log *entity*'s reason if it changed, or that its value is back."""
        entity_id = entity.entity_id
        state = hass.states.get(entity_id) if entity_id else None
        if state is None:
            # Disabled or removed: nothing to report, and nothing to recover.
            self._forget(entity_id)
            return
        if state.state == STATE_UNAVAILABLE:
            return
        if state.state != STATE_UNKNOWN:
            self._unexplained.pop(entity_id, None)
            if self._reported.pop(entity_id, None) is not None:
                async_log("info", "%s has a value again", entity_id)
            return

        code: str | None = getattr(entity, "unknown_reason", None)
        if code:
            self._unexplained.pop(entity_id, None)
            if self._reported.get(entity_id) != code:
                self._reported[entity_id] = code
                reason = classify(code)
                async_log(
                    LOG_LEVELS[reason.kind],
                    "%s is unknown: %s (%s). %s",
                    entity_id,
                    code,
                    reason.kind.value,
                    reason.sentence,
                )
            return

        count = self._unexplained.get(entity_id, 0) + 1
        self._unexplained[entity_id] = count
        if (
            count >= UNEXPLAINED_CHECKS
            and self._reported.get(entity_id) != _UNEXPLAINED
        ):
            self._reported[entity_id] = _UNEXPLAINED
            async_log(
                "warning",
                "%s is unknown and gives no reason. Check the entities it "
                "reads, and report it to HSEM if they are fine.",
                entity_id,
            )

    def _forget(self, entity_id: str | None) -> None:
        """Drop what was reported for an entity that has no state."""
        if entity_id:
            self._reported.pop(entity_id, None)
            self._unexplained.pop(entity_id, None)
