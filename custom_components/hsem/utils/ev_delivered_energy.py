"""Conservative delivered-energy tracking for stale EV SoC telemetry.

Vehicle integrations commonly publish state of charge much less frequently than
charger power. This helper integrates bounded, measured AC power between valid
samples and carries the resulting battery-energy credit until the reported SoC
catches up. It is intentionally in-memory and pure Python: a restart or unsafe
session/SoC identity loses credit instead of carrying it to another vehicle.
Invalid power, time, or a long gap preserves already validated credit while
breaking the integration baseline so the unsafe interval adds no energy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

_SOC_EPSILON_PCT = 1e-6


@dataclass(frozen=True, slots=True)
class EVDeliveredEnergyEstimate:
    """One bounded effective-SoC estimate."""

    effective_soc_pct: float | None
    credit_kwh: float = 0.0


class EVDeliveredEnergyTracker:
    """Track delivered EV battery energy between coarse SoC updates."""

    def __init__(self) -> None:
        """Initialise an empty, fail-closed tracker."""
        self._last_timestamp: float | None = None
        self._last_power_w: float | None = None
        self._last_reported_soc_pct: float | None = None
        self._battery_capacity_kwh: float | None = None
        self._credit_kwh: float = 0.0

    def reset(self) -> None:
        """Forget all session identity, timing, and delivered-energy credit."""
        self._last_timestamp = None
        self._last_power_w = None
        self._last_reported_soc_pct = None
        self._battery_capacity_kwh = None
        self._credit_kwh = 0.0

    def update(
        self,
        *,
        now: datetime,
        connected: bool,
        charging: bool,
        power_w: float | None,
        reported_soc_pct: float | None,
        target_soc_pct: float | None,
        battery_capacity_kwh: float,
        charger_efficiency_pct: float,
        max_power_w: float,
        allow_charge_past_target: bool,
        max_gap_seconds: float,
    ) -> EVDeliveredEnergyEstimate:
        """Return effective SoC after one telemetry observation.

        The integration uses the trapezoid between two valid charging-power
        samples. No energy is credited across an excessive gap or when either
        endpoint is invalid. Invalid timing or power preserves earlier valid
        credit but breaks/advances the integration baseline so the unsafe
        interval contributes zero. A reported SoC advance rebases only the
        portion the telemetry explains; any still-unreported delivered energy
        remains.
        """
        raw_soc = _finite_in_range(reported_soc_pct, 0.0, 100.0)
        capacity_kwh = _finite_positive(battery_capacity_kwh)
        target_soc = _finite_in_range(target_soc_pct, 0.0, 100.0)
        efficiency_pct = _finite_in_range(
            charger_efficiency_pct,
            _SOC_EPSILON_PCT,
            100.0,
        )
        safe_max_power_w = _finite_positive(max_power_w)
        safe_max_gap_seconds = _finite_positive(max_gap_seconds)

        # A confirmed connection and valid battery identity are prerequisites.
        # Dropping credit is conservative: the next valid sample starts a new
        # in-memory session without carrying energy to another vehicle.
        if (
            not connected
            or raw_soc is None
            or capacity_kwh is None
            or target_soc is None
            or efficiency_pct is None
            or safe_max_power_w is None
            or safe_max_gap_seconds is None
        ):
            self.reset()
            return EVDeliveredEnergyEstimate(effective_soc_pct=raw_soc)

        timestamp = _aware_timestamp(now)
        current_power_w = _valid_charging_power_w(
            charging=charging,
            power_w=power_w,
            max_power_w=safe_max_power_w,
        )

        # A capacity change makes the stored kWh-to-SoC mapping incompatible.
        if self._battery_capacity_kwh is not None and not math.isclose(
            self._battery_capacity_kwh,
            capacity_kwh,
            rel_tol=0.0,
            abs_tol=1e-9,
        ):
            self.reset()

        previous_soc = self._last_reported_soc_pct
        if previous_soc is None:
            self._set_observation(
                timestamp=timestamp,
                power_w=current_power_w,
                reported_soc_pct=raw_soc,
                battery_capacity_kwh=capacity_kwh,
            )
            return EVDeliveredEnergyEstimate(effective_soc_pct=raw_soc)

        # A backwards SoC jump is an identity/correction boundary. Never carry
        # credited energy into the lower reading.
        if raw_soc + _SOC_EPSILON_PCT < previous_soc:
            self.reset()
            self._set_observation(
                timestamp=timestamp,
                power_w=current_power_w,
                reported_soc_pct=raw_soc,
                battery_capacity_kwh=capacity_kwh,
            )
            return EVDeliveredEnergyEstimate(effective_soc_pct=raw_soc)

        delivered_kwh = 0.0
        observation_timestamp = timestamp
        if self._last_timestamp is not None and (
            timestamp is None or timestamp < self._last_timestamp
        ):
            # A missing or reversed clock cannot become the start of a later
            # integration interval. Preserve validated credit, but require
            # the next valid sample to establish a fresh baseline. An equal
            # timestamp is a valid zero-duration observation: keeping it
            # lets a same-second coordinator refresh update the power/SoC
            # endpoint without discarding the following measurable interval
            # (issue #797).
            observation_timestamp = None
        if (
            timestamp is not None
            and self._last_timestamp is not None
            and current_power_w is not None
            and self._last_power_w is not None
            and charging
        ):
            elapsed_seconds = timestamp - self._last_timestamp
            if 0.0 < elapsed_seconds <= safe_max_gap_seconds:
                average_power_w = (self._last_power_w + current_power_w) / 2.0
                delivered_kwh = (
                    average_power_w
                    * elapsed_seconds
                    / 3_600_000.0
                    * efficiency_pct
                    / 100.0
                )

        previous_effective_kwh = previous_soc / 100.0 * capacity_kwh + self._credit_kwh
        candidate_effective_kwh = previous_effective_kwh + delivered_kwh
        reported_kwh = raw_soc / 100.0 * capacity_kwh

        # Preserve only energy not yet represented by the new telemetry. When
        # the report catches or overtakes the estimate, the residual is zero.
        credit_kwh = max(candidate_effective_kwh - reported_kwh, 0.0)
        credit_ceiling_soc = 100.0 if allow_charge_past_target else target_soc
        credit_ceiling_soc = max(raw_soc, credit_ceiling_soc)
        max_credit_kwh = max(credit_ceiling_soc - raw_soc, 0.0) / 100.0 * capacity_kwh
        self._credit_kwh = min(credit_kwh, max_credit_kwh)

        effective_soc = min(
            raw_soc + self._credit_kwh / capacity_kwh * 100.0,
            credit_ceiling_soc,
            100.0,
        )
        self._set_observation(
            timestamp=observation_timestamp,
            power_w=current_power_w,
            reported_soc_pct=raw_soc,
            battery_capacity_kwh=capacity_kwh,
        )
        return EVDeliveredEnergyEstimate(
            effective_soc_pct=effective_soc,
            credit_kwh=self._credit_kwh,
        )

    def _set_observation(
        self,
        *,
        timestamp: float | None,
        power_w: float | None,
        reported_soc_pct: float,
        battery_capacity_kwh: float,
    ) -> None:
        """Store the safe endpoint for the next bounded interval."""
        self._last_timestamp = timestamp
        self._last_power_w = power_w
        self._last_reported_soc_pct = reported_soc_pct
        self._battery_capacity_kwh = battery_capacity_kwh


def _aware_timestamp(value: datetime) -> float | None:
    """Return a finite timestamp only for an aware datetime."""
    try:
        if value.tzinfo is None or value.utcoffset() is None:
            return None
        timestamp = value.timestamp()
    except AttributeError, OverflowError, OSError, ValueError:
        return None
    return timestamp if math.isfinite(timestamp) else None


def _finite_positive(value: float) -> float | None:
    """Return a finite positive float, otherwise ``None``."""
    try:
        converted = float(value)
    except TypeError, ValueError:
        return None
    return converted if math.isfinite(converted) and converted > 0.0 else None


def _finite_in_range(
    value: float | None,
    minimum: float,
    maximum: float,
) -> float | None:
    """Return a finite float inside an inclusive range."""
    if value is None:
        return None
    try:
        converted = float(value)
    except TypeError, ValueError:
        return None
    if not math.isfinite(converted) or not minimum <= converted <= maximum:
        return None
    return converted


def _valid_charging_power_w(
    *,
    charging: bool,
    power_w: float | None,
    max_power_w: float,
) -> float | None:
    """Return a bounded charging-power endpoint, or ``None`` to skip it."""
    if not charging or power_w is None:
        return None
    try:
        converted = float(power_w)
    except TypeError, ValueError:
        return None
    if not math.isfinite(converted) or converted < 0.0 or converted > max_power_w:
        return None
    return converted
