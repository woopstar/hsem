"""OCPP 1.6 MeterValues configuration and per-phase current (issue #1391).

HSEM used to take whatever ``MeterValues`` a charger sent by default. This
module decides which configuration keys to write after a
``GetConfiguration`` reply, so the charger samples at least once a minute
and includes ``Current.Import`` per phase. It also derives how many phases
the car actually draws on from those readings.
"""

from __future__ import annotations

SAMPLE_INTERVAL_KEY = "MeterValueSampleInterval"
SAMPLED_DATA_KEY = "MeterValuesSampledData"
SAMPLED_DATA_MAX_LENGTH_KEY = "MeterValuesSampledDataMaxLength"

# Longest acceptable sample interval. A charger already sampling faster is
# left alone, and 0 (sampling disabled) is replaced.
TARGET_SAMPLE_INTERVAL_S = 60

# Measurands HSEM reads, most important first so a charger with a short
# MeterValuesSampledDataMaxLength keeps the ones that matter.
TARGET_MEASURANDS = (
    "Energy.Active.Import.Register",
    "Power.Active.Import",
    "Current.Import",
)

PHASES = ("L1", "L2", "L3")

# A phase counts as active above this current. Idle phases often report a
# few tenths of an ampere of noise.
PHASE_ACTIVE_THRESHOLD_A = 1.0


def _as_int(raw: str | None) -> int | None:
    """Parse an OCPP configuration value as an integer.

    Args:
        raw: The value as reported, or ``None``.

    Returns:
        The integer, or ``None`` when missing or not a number.
    """
    try:
        return int(str(raw).strip())
    except ValueError, TypeError:
        return None


def meter_config_changes(
    configuration_keys: dict[str, str], readonly_keys: set[str]
) -> dict[str, str]:
    """Return the MeterValues configuration HSEM should write.

    A key is written only when the charger reports it, it is not
    read-only, and its value differs from what HSEM needs.

    Args:
        configuration_keys: The charger's reported configuration.
        readonly_keys: Keys the charger reported as read-only.

    Returns:
        Mapping of key to new value. Empty when nothing needs writing.
    """
    changes: dict[str, str] = {}

    if (
        SAMPLE_INTERVAL_KEY in configuration_keys
        and SAMPLE_INTERVAL_KEY not in readonly_keys
    ):
        interval = _as_int(configuration_keys[SAMPLE_INTERVAL_KEY])
        if interval is None or interval <= 0 or interval > TARGET_SAMPLE_INTERVAL_S:
            changes[SAMPLE_INTERVAL_KEY] = str(TARGET_SAMPLE_INTERVAL_S)

    if SAMPLED_DATA_KEY in configuration_keys and SAMPLED_DATA_KEY not in readonly_keys:
        wanted = list(TARGET_MEASURANDS)
        max_length = _as_int(configuration_keys.get(SAMPLED_DATA_MAX_LENGTH_KEY))
        if max_length is not None and max_length > 0:
            wanted = wanted[:max_length]
        current = {
            item.strip()
            for item in configuration_keys[SAMPLED_DATA_KEY].split(",")
            if item.strip()
        }
        if not set(wanted) <= current:
            changes[SAMPLED_DATA_KEY] = ",".join(wanted)

    return changes


def active_phase_count(phase_currents_a: dict[str, float]) -> int | None:
    """Return how many phases draw current.

    Args:
        phase_currents_a: Latest current per phase (``L1``/``L2``/``L3``).

    Returns:
        The number of phases above :data:`PHASE_ACTIVE_THRESHOLD_A`, or
        ``None`` when the charger reports no per-phase current.
    """
    if not phase_currents_a:
        return None
    return sum(
        1 for amps in phase_currents_a.values() if amps > PHASE_ACTIVE_THRESHOLD_A
    )


__all__ = [
    "PHASES",
    "SAMPLED_DATA_KEY",
    "SAMPLE_INTERVAL_KEY",
    "active_phase_count",
    "meter_config_changes",
]
