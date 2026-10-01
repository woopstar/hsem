"""Which recommendation slots received price data (issues #1196 and #1217).

Every recommendation slot starts with ``import_price`` and ``export_price``
at ``0.0`` and the populator only writes the slots a source point covers.  The
value alone therefore cannot tell "the source published 0.0" from "the source
published nothing".  This object carries that difference from the populator to
:func:`~custom_components.hsem.coordinator_builder.build_planner_input`, which
leaves uncovered slots out of the planner input so the planner's own
missing-data handling runs: the missing-price estimate of issue #1002 and the
``DataQuality`` missing-hour fields.

It is deliberately not a field of ``HourlyRecommendation``: that list is
published as a sensor attribute, so a per-slot flag would add a key to every
slot (issue #1099).

The 6.3.x line tracks prices only.  PV coverage (``main``, issue #1196) needs
the per-slot PV entries of issue #1191 stage 2, which 6.3.x does not have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.utils.datetime_utils import utc_key


@dataclass(frozen=True)
class ForecastCoverage:
    """Slot starts, as UTC instants, that each price source wrote a value to.

    Attributes:
        import_price: Slots that received an import price.
        export_price: Slots that received an export price.
    """

    import_price: frozenset[datetime] = frozenset()
    export_price: frozenset[datetime] = frozenset()

    def has_price(self, slot_start: datetime) -> bool:
        """Return whether the slot has both an import and an export price.

        The planner prices a slot with a pair, so one missing side makes the
        slot's price missing.
        """
        key = utc_key(slot_start)
        return key in self.import_price and key in self.export_price
