"""Turn two consecutive meter or power readings into the energy between them.

Shared by the trackers that are fed one sample per coordinator cycle (the
performance tracker and the energy tracker), so a meter glitch is recognised
the same way everywhere.
"""

from __future__ import annotations

#: Largest grid power a meter delta may imply before it is taken for a meter
#: glitch and the interval is left unobserved.
MAX_METER_KW = 50.0

#: Allowance on top of that for a meter that counts in coarse steps: two
#: cycles a few seconds apart can straddle a whole step.
METER_STEP_KWH = 2.0


def meter_delta(
    before: float | None, after: float | None, hours: float
) -> float | None:
    """Return a cumulative meter's increase, or ``None`` when it is unusable.

    Args:
        before: The meter reading at the start of the interval (kWh).
        after: The meter reading at the end of the interval (kWh).
        hours: Length of the interval in hours.

    Returns:
        The increase in kWh, or ``None`` when a reading is missing, the meter
        ran backwards or the increase implies an impossible power.
    """
    if before is None or after is None:
        return None
    delta = after - before
    if delta < -1e-9 or delta > MAX_METER_KW * hours + METER_STEP_KWH:
        return None
    return max(delta, 0.0)


def mean_power_kwh(
    before_w: float | None, after_w: float | None, hours: float
) -> float | None:
    """Return the energy of a power reading held between two samples.

    Args:
        before_w: Power at the start of the interval (W).
        after_w: Power at the end of the interval (W).
        hours: Length of the interval in hours.

    Returns:
        The energy in kWh, or ``None`` when a reading is missing.
    """
    if before_w is None or after_w is None:
        return None
    return (before_w + after_w) / 2.0 * hours / 1000.0
