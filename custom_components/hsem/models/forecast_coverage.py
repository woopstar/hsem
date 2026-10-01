"""Which recommendation slots received price and PV data (issue #1196).

Every recommendation slot starts with ``import_price``, ``export_price`` and
``solcast_pv_estimate_kwh`` at ``0.0`` and the populator only writes the slots
a source point covers.  The value alone therefore cannot tell "the source
published 0.0" from "the source published nothing".  This object carries that
difference from the populator to
:func:`~custom_components.hsem.coordinator_builder.build_planner_input`, which
leaves uncovered slots out of the planner input so the planner's own
missing-data handling runs: the missing-price estimate of issue #1002 and the
``DataQuality`` missing-hour fields.

It is deliberately not a field of ``HourlyRecommendation``: that list is
published as a sensor attribute, so a per-slot flag would add a key to every
slot (issue #1099).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from custom_components.hsem.utils.datetime_utils import utc_key


@dataclass(frozen=True)
class ForecastCoverage:
    """Slot starts, as UTC instants, that each source wrote a value to.

    Attributes:
        import_price: Slots that received an import price.
        export_price: Slots that received an export price.
        pv: Slots that received a PV forecast.
        pv_source_configured: Whether any PV forecast sensor is configured.
            Without one no slot is ever covered, and that is not a gap.
    """

    import_price: frozenset[datetime] = frozenset()
    export_price: frozenset[datetime] = frozenset()
    pv: frozenset[datetime] = frozenset()
    pv_source_configured: bool = False

    def has_price(self, slot_start: datetime) -> bool:
        """Return whether the slot has both an import and an export price.

        The planner prices a slot with a pair, so one missing side makes the
        slot's price missing.
        """
        key = utc_key(slot_start)
        return key in self.import_price and key in self.export_price

    def has_pv(self, slot_start: datetime) -> bool:
        """Return whether the slot's PV value is data rather than a default.

        Always ``True`` without a configured PV source: such a setup has no
        PV forecast at all, and reporting every hour as missing on every
        cycle would be noise.
        """
        return not self.pv_source_configured or utc_key(slot_start) in self.pv
