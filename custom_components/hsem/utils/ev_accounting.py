"""Canonical helpers for EV load accounting against the house baseline."""

from __future__ import annotations


def normalized_baseline_includes_ev(
    *,
    raw_house_meter_includes_ev: bool,
    ev_power_entity: object,
) -> bool:
    """Return whether one EV remains embedded in planner house-load averages.

    The raw CT-position setting describes the instantaneous site meter. HSEM's
    generated house-consumption history subtracts each EV that has a configured
    power entity before accumulating utility-meter and rolling-average values.
    Only an EV without such telemetry can therefore remain in the normalized
    planner baseline when the raw meter is EV-inclusive.
    """
    telemetry_configured = isinstance(ev_power_entity, str) and bool(
        ev_power_entity.strip()
    )
    return raw_house_meter_includes_ev and not telemetry_configured
