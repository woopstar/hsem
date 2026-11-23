"""Round-trip efficiency of the battery, from a period's summed energy.

The energy tracker measures the round trip on two sides (issue #1450):

- **Battery side**: the battery's own charge/discharge power.  Inverter
  conversion loss is not in it.
- **AC side**: an optional sensor of the battery's flow on the AC side of the
  inverter.  This is the loss the meter sees.

Both use the same formula, so it lives here, apart from
:mod:`~custom_components.hsem.models.energy_tracker` and its file-size limit.
The AC side has two gates more.  Its sensor is picked by the user, and one
that reports the power without a sign books everything as charge: the AC
side therefore also needs a minimum of discharged energy and an efficiency
above :data:`MIN_AC_ROUND_TRIP_EFFICIENCY`.  The battery side keeps the gates
it had.
"""

from __future__ import annotations

from typing import Any

from custom_components.hsem.utils.misc import round_or_none

#: Measured battery charge below which the round-trip loss is not given.
MIN_ROUND_TRIP_CHARGED_KWH = 20.0

#: The same minimum as a multiple of the rated capacity; the larger applies.
MIN_ROUND_TRIP_CAPACITY_MULTIPLE = 5.0

#: How far off the stored-energy change may be, as a share of itself.  It is
#: the SoC change times the nameplate capacity: an aged battery holds less,
#: and what it holds comes out with a loss the measurement is yet to find.
STORED_DELTA_RELATIVE_ERROR = 0.15

#: Largest uncertainty of the round-trip loss still published, as a share of
#: the charged energy.  It is met while the stored-energy change is at most a
#: tenth of the charged energy.
MAX_ROUND_TRIP_UNCERTAINTY = 0.015

#: Highest measured round-trip efficiency still taken for a measurement.
MAX_ROUND_TRIP_EFFICIENCY = 1.02

#: Lowest AC-side round-trip efficiency still taken for a measurement, the
#: bound the efficiency learner uses (``MIN_ROUND_TRIP``).
MIN_AC_ROUND_TRIP_EFFICIENCY = 0.5

#: Why a round-trip figure is unknown.
REASON_NO_DATA = "no_data"
REASON_BATTERY_POWER_NOT_MEASURED = "battery_power_not_measured"
#: No AC-side battery power sensor is configured.
REASON_AC_BATTERY_POWER_NOT_CONFIGURED = "ac_battery_power_not_configured"
#: The configured sensor's unit is not a power unit, or it has none.
REASON_AC_BATTERY_POWER_UNIT_NOT_POWER = "ac_battery_power_unit_not_power"
#: The configured sensor gave no usable reading: missing, not numeric, stale.
REASON_AC_BATTERY_POWER_NO_READINGS = "ac_battery_power_no_readings"
#: The sensor gave readings, but never with a SoC and a rated capacity.
REASON_AC_SOC_OR_CAPACITY_MISSING = "ac_soc_or_capacity_missing"
REASON_THROUGHPUT_BELOW_MINIMUM = "throughput_below_minimum"
REASON_SOC_CHANGE_TOO_LARGE = "soc_change_too_large"
REASON_IMPLAUSIBLE_RATIO = "implausible_ratio"


def battery_unknown_reason(
    charged: float, discharged: float, *, has_days: bool
) -> str | None:
    """Return why a period has no battery-side measurement, or ``None``.

    Args:
        charged: Energy into the battery, from the battery power (kWh).
        discharged: Energy out of the battery, from the battery power (kWh).
        has_days: Whether the period has any daily record.
    """
    if not has_days:
        return REASON_NO_DATA
    if charged + discharged <= 1e-9:
        return REASON_BATTERY_POWER_NOT_MEASURED
    return None


def ac_unknown_reason(
    ac_power_hours: float, ac_hours: float, *, has_days: bool, sensor_reason: str
) -> str | None:
    """Return why a period has no AC-side measurement, or ``None``.

    Args:
        ac_power_hours: Hours in which the AC-side sensor gave readings.
        ac_hours: Hours of those in which the SoC change was known too, so
            the AC-side flows were booked.
        has_days: Whether the period has any daily record.
        sensor_reason: Why the sensor gives no readings as it is set up now:
            not configured, a unit that is not power, or no readings.
    """
    if ac_hours > 1e-9:
        return None
    if ac_power_hours > 1e-9:
        return REASON_AC_SOC_OR_CAPACITY_MISSING
    if sensor_reason != REASON_AC_BATTERY_POWER_NO_READINGS:
        return sensor_reason
    return REASON_AC_BATTERY_POWER_NO_READINGS if has_days else REASON_NO_DATA


def round_trip_efficiency(
    charged: float,
    discharged: float,
    stored_delta: float,
    rated_capacity_kwh: float,
    *,
    unknown_reason: str | None,
    ac_side: bool = False,
) -> tuple[float | None, float | None, str | None]:
    """Return the measured round-trip efficiency, its uncertainty, or a reason.

    ``(discharged + stored-energy change) ÷ charged``: energy still in the
    battery at the end of the period counts as delivered, and energy that was
    already there at the start is taken off what was delivered.

    The stored-energy change is the weak term: the SoC times the nameplate
    capacity, taken at full value.  Its possible error, as a share of the
    charged energy, is the uncertainty.  The efficiency is only given once
    enough has been charged and that uncertainty is small.

    Args:
        charged: Energy into the battery over the period (kWh).
        discharged: Energy out of the battery over the period (kWh).
        stored_delta: Change in stored energy over the same intervals (kWh).
        rated_capacity_kwh: Nameplate capacity, or zero when unknown.
        unknown_reason: Why the period has no measurement, or ``None``.
        ac_side: Apply the two AC-side gates: the minimum also holds for the
            discharged energy, and an efficiency below
            :data:`MIN_AC_ROUND_TRIP_EFFICIENCY` is no measurement.

    Returns:
        The efficiency and its uncertainty as fractions, and ``None``; or
        ``None``, ``None`` and the reason.
    """
    if unknown_reason is not None:
        return None, None, unknown_reason
    minimum = max(
        MIN_ROUND_TRIP_CHARGED_KWH,
        MIN_ROUND_TRIP_CAPACITY_MULTIPLE * rated_capacity_kwh,
    )
    if charged < minimum or (ac_side and discharged < minimum):
        return None, None, REASON_THROUGHPUT_BELOW_MINIMUM
    uncertainty = abs(stored_delta) * STORED_DELTA_RELATIVE_ERROR / charged
    if uncertainty > MAX_ROUND_TRIP_UNCERTAINTY:
        return None, None, REASON_SOC_CHANGE_TOO_LARGE
    efficiency = (discharged + stored_delta) / charged
    lowest = MIN_AC_ROUND_TRIP_EFFICIENCY if ac_side else 0.0
    if efficiency <= lowest or efficiency > MAX_ROUND_TRIP_EFFICIENCY:
        return None, None, REASON_IMPLAUSIBLE_RATIO
    return min(efficiency, 1.0), uncertainty, None


def round_trip_summary(
    prefix: str,
    charged: float,
    discharged: float,
    stored_delta: float,
    rated_capacity_kwh: float,
    *,
    unknown_reason: str | None,
) -> dict[str, Any]:
    """Return what a month publishes about one side's round trip.

    Args:
        prefix: Prefix of every key: empty for the battery side, ``ac_`` for
            the AC side.
        charged: Energy into the battery over the month (kWh).
        discharged: Energy out of the battery over the month (kWh).
        stored_delta: Change in stored energy over the same intervals (kWh).
        rated_capacity_kwh: Nameplate capacity, or zero when unknown.
        unknown_reason: Why the month has no measurement, or ``None``.

    Returns:
        The efficiency, the loss and its uncertainty in percent (``None``
        when unknown), the reason, and the three energy sums behind them.
    """
    efficiency, uncertainty, reason = round_trip_efficiency(
        charged,
        discharged,
        stored_delta,
        rated_capacity_kwh,
        unknown_reason=unknown_reason,
        ac_side=bool(prefix),
    )
    return {
        f"{prefix}round_trip_efficiency": round_or_none(
            None if efficiency is None else efficiency * 100.0, 1
        ),
        f"{prefix}round_trip_loss": round_or_none(
            None if efficiency is None else (1.0 - efficiency) * 100.0, 1
        ),
        f"{prefix}round_trip_loss_uncertainty": round_or_none(
            None if uncertainty is None else uncertainty * 100.0, 1
        ),
        f"{prefix}round_trip_loss_reason": reason,
        f"{prefix}round_trip_charged_kwh": round(charged, 3),
        f"{prefix}round_trip_discharged_kwh": round(discharged, 3),
        f"{prefix}round_trip_stored_delta_kwh": round(stored_delta, 3),
    }
