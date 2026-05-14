"""Diagnostics dump utilities for HSEM.

Single responsibility: produce a safe, JSON-serialisable snapshot of one
HSEM planning cycle that can be:

- Attached to a Home Assistant diagnostics report (``async_get_config_entry_diagnostics``).
- Stored to disk for offline replay in the test suite.
- Shared in GitHub issue reports without leaking credentials.

Redaction
---------
Any field whose name or value looks like an HA entity-id (``domain.name``),
token, password, or other sensitive identifier is either omitted or replaced
with ``"**REDACTED**"`` before the dump is returned.

Specifically, Huawei Solar entity IDs listed in the live state (e.g.
``sensor.batteries_state_of_capacity``) and config entry entity references are
replaced so that the dump does not expose the user's HA entity namespace.

Reproducibility
---------------
The serialised :class:`~custom_components.hsem.models.planner_inputs.PlannerInput`
is included verbatim (without entity IDs) so that the planner engine can be
re-run deterministically in tests:

    from custom_components.hsem.utils.diagnostics import load_planner_input_from_dump
    from custom_components.hsem.planner import run_planner

    inp = load_planner_input_from_dump(dump)
    output = run_planner(inp)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict
from datetime import datetime, time
from typing import Any

from custom_components.hsem.models.planner_inputs import (
    BatteryScheduleInput,
    HourlyConsumptionAverage,
    PlannerInput,
    PricePoint,
    SolcastSlot,
)
from custom_components.hsem.models.planner_outputs import PlannerOutput

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------

# Matches typical HA entity IDs: domain.entity_name (e.g. sensor.foo_bar_123)
_ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$", re.IGNORECASE)

# Field name substrings that indicate sensitive config values to be redacted.
_SENSITIVE_FIELD_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "token",
        "password",
        "secret",
        "api_key",
        "access_key",
        "client_id",
        "client_secret",
    }
)

_REDACTED = "**REDACTED**"


def _is_sensitive_key(key: str) -> bool:
    """Return ``True`` when *key* looks like it holds a secret value.

    Args:
        key: The field or dict key name to inspect.

    Returns:
        ``True`` if any sensitive substring is found in the lower-cased key.
    """
    lower = key.lower()
    return any(sub in lower for sub in _SENSITIVE_FIELD_SUBSTRINGS)


def _redact_value(value: Any) -> Any:
    """Replace HA entity-id strings and other sensitive values with a placeholder.

    Only string values that look like HA entity IDs (``domain.entity_name``) are
    replaced; numeric, bool, list, and dict values are returned unchanged so that
    the dump retains all data needed to reproduce planner behaviour.

    Args:
        value: The value to inspect and potentially redact.

    Returns:
        The original value, or ``_REDACTED`` if the value looks like a secret.
    """
    if isinstance(value, str) and _ENTITY_ID_RE.match(value):
        return _REDACTED
    return value


def redact_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact sensitive keys and HA entity-id values from *data*.

    Rules:
    - Any key matching ``_is_sensitive_key`` → value replaced with ``_REDACTED``.
    - Any string value matching the HA entity-id pattern → replaced with
      ``_REDACTED``.
    - Lists are walked item-by-item; nested dicts are recursed into.

    Args:
        data: A JSON-serialisable dictionary (from ``dataclasses.asdict`` or
              similar).

    Returns:
        A new dictionary with the same structure but with sensitive data replaced.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if _is_sensitive_key(key):
            result[key] = _REDACTED
        elif isinstance(value, dict):
            result[key] = redact_dict(value)
        elif isinstance(value, list):
            result[key] = [
                redact_dict(item) if isinstance(item, dict) else _redact_value(item)
                for item in value
            ]
        else:
            result[key] = _redact_value(value)
    return result


# ---------------------------------------------------------------------------
# PlannerInput serialisation / deserialisation
# ---------------------------------------------------------------------------


def _time_to_str(t: time | None) -> str | None:
    """Serialise a :class:`datetime.time` to ``HH:MM:SS`` or ``None``.

    Args:
        t: A time instance or ``None``.

    Returns:
        ``"HH:MM:SS"`` string, or ``None``.
    """
    return t.strftime("%H:%M:%S") if t is not None else None


def _planner_input_to_dict(inp: PlannerInput) -> dict[str, Any]:
    """Convert a :class:`PlannerInput` to a JSON-safe dictionary.

    :class:`datetime.time` values inside ``battery_schedules`` are serialised
    to ``"HH:MM:SS"`` strings.  All other fields are plain Python primitives
    already.

    Args:
        inp: The planner input to serialise.

    Returns:
        A JSON-serialisable dictionary.
    """
    raw = asdict(inp)

    # Patch datetime.time objects that asdict() cannot serialise to JSON.
    for sched in raw.get("battery_schedules", []):
        sched["start"] = (
            _time_to_str(inp.battery_schedules[0].start)
            if False
            else (
                sched["start"].strftime("%H:%M:%S")
                if isinstance(sched["start"], time)
                else sched["start"]
            )
        )
        sched["end"] = (
            sched["end"].strftime("%H:%M:%S")
            if isinstance(sched["end"], time)
            else sched["end"]
        )

    # Redact any stray entity-id strings that found their way into ``extra``.
    if "extra" in raw:
        raw["extra"] = redact_dict(raw["extra"])

    return raw


def _planner_input_from_dict(data: dict[str, Any]) -> PlannerInput:
    """Reconstruct a :class:`PlannerInput` from a serialised dictionary.

    Inverse of :func:`_planner_input_to_dict`.  Handles the ``HH:MM:SS`` →
    :class:`datetime.time` conversion for battery schedules.

    Args:
        data: A dictionary previously produced by :func:`_planner_input_to_dict`.

    Returns:
        A fully populated :class:`PlannerInput`.
    """
    inp_data = dict(data)

    # Reconstruct nested dataclass lists.
    inp_data["consumption_averages"] = [
        HourlyConsumptionAverage(**item)
        for item in inp_data.get("consumption_averages", [])
    ]
    inp_data["price_points"] = [
        PricePoint(**item) for item in inp_data.get("price_points", [])
    ]
    inp_data["solcast_slots"] = [
        SolcastSlot(**item) for item in inp_data.get("solcast_slots", [])
    ]

    schedules = []
    for raw_sched in inp_data.get("battery_schedules", []):
        raw_sched = dict(raw_sched)
        for field_name in ("start", "end"):
            val = raw_sched.get(field_name)
            if isinstance(val, str):
                raw_sched[field_name] = datetime.strptime(val, "%H:%M:%S").time()
        schedules.append(BatteryScheduleInput(**raw_sched))
    inp_data["battery_schedules"] = schedules

    # ``battery_max_discharge_power_w`` may be None (nullable float).
    if (
        "battery_max_discharge_power_w" in inp_data
        and inp_data["battery_max_discharge_power_w"] is not None
    ):
        inp_data["battery_max_discharge_power_w"] = float(
            inp_data["battery_max_discharge_power_w"]
        )

    return PlannerInput(**inp_data)


# ---------------------------------------------------------------------------
# PlannerOutput summarisation
# ---------------------------------------------------------------------------


def _slot_to_dict(slot: Any) -> dict[str, Any]:
    """Serialise a :class:`~custom_components.hsem.models.planner_outputs.PlannedSlot`.

    Converts :class:`datetime` fields to ISO strings for JSON portability.

    Args:
        slot: A ``PlannedSlot`` instance.

    Returns:
        A JSON-safe dict.
    """
    return {
        "start": slot.start.isoformat(),
        "end": slot.end.isoformat(),
        "import_price": round(slot.price.import_price, 5),
        "export_price": round(slot.price.export_price, 5),
        "solcast_pv_estimate": round(slot.solcast_pv_estimate, 3),
        "avg_house_consumption": round(slot.avg_house_consumption, 3),
        "estimated_net_consumption": round(slot.estimated_net_consumption, 3),
        "estimated_cost": round(slot.estimated_cost, 4),
        "estimated_battery_soc": round(slot.estimated_battery_soc, 1),
        "batteries_charged": round(slot.batteries_charged, 3),
        "batteries_discharged": round(slot.batteries_discharged, 3),
        "grid_import_kwh": round(slot.grid_import_kwh, 3),
        "grid_export_kwh": round(slot.grid_export_kwh, 3),
        "recommendation": slot.recommendation,
    }


def _window_to_dict(window: Any) -> dict[str, Any]:
    """Serialise a charge or discharge window to a plain dict.

    Args:
        window: A ``ChargeWindow`` or ``DischargeWindow`` instance.

    Returns:
        A JSON-safe dict.
    """
    return {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "recommendation": window.recommendation,
        **{
            k: round(v, 4)
            for k, v in vars(window).items()
            if k not in ("start", "end", "recommendation")
            and isinstance(v, (int, float))
        },
    }


def _apply_summary_to_dict(summary: Any) -> dict[str, Any] | None:
    """Serialise a :class:`~custom_components.hsem.utils.inverter_verify.CycleApplySummary`.

    Entity IDs are redacted; desired/actual values are retained so bug
    reports show what HSEM tried to write vs. what the inverter returned.

    Args:
        summary: A ``CycleApplySummary`` instance or ``None``.

    Returns:
        A JSON-safe dict, or ``None`` when *summary* is ``None``.
    """
    if summary is None:
        return None
    return {
        "last_updated": summary.last_updated,
        "overall_status": str(summary.overall_status),
        "results": [
            {
                "entity_id": _REDACTED,
                "desired": r.desired,
                "actual": r.actual,
                "status": str(r.status),
                "attempts": r.attempts,
                "error_message": r.error_message,
            }
            for r in summary.results
        ],
    }


def _planner_output_summary(output: PlannerOutput) -> dict[str, Any]:
    """Produce a condensed, JSON-safe summary of a :class:`PlannerOutput`.

    Includes the selected plan slots, charge/discharge windows, explanation,
    rejected plans, data quality, warnings, and the cost breakdown.  The
    full ``time_series_index`` is omitted (too large and not needed for
    reproducibility).

    Args:
        output: The planner output to summarise.

    Returns:
        A JSON-safe dictionary.
    """
    candidates_summary = []
    for cand in output.candidates:
        try:
            entry: dict[str, Any] = {
                "name": getattr(cand, "name", str(cand)),
                "is_valid": getattr(cand, "is_valid", None),
                "rejection_reason": getattr(cand, "rejection_reason", None),
            }
            cost = getattr(cand, "cost", None)
            if cost is not None:
                entry["cost"] = round(float(cost), 4)
            candidates_summary.append(entry)
        except Exception:  # noqa: BLE001 — never crash the diagnostics path
            candidates_summary.append({"name": repr(cand)})

    plan_cost: dict[str, Any] | None = None
    if output.plan_cost is not None:
        try:
            plan_cost = {
                k: round(v, 4) if isinstance(v, float) else v
                for k, v in vars(output.plan_cost).items()
            }
        except Exception:  # noqa: BLE001
            plan_cost = {"error": "could not serialise plan_cost"}

    return {
        "current_recommendation": output.current_recommendation,
        "battery_soc_at_end": round(output.battery_soc_at_end, 1),
        "required_capacity_kwh": round(output.required_capacity_kwh, 3),
        "missing_inputs": list(output.missing_inputs),
        "warnings": list(output.warnings),
        "data_quality": output.data_quality.as_dict(),
        "explanation": output.explanation.as_dict(),
        "plan_cost": plan_cost,
        "candidates": candidates_summary,
        "slots": [_slot_to_dict(s) for s in output.slots],
        "charge_windows": [_window_to_dict(w) for w in output.charge_windows],
        "discharge_windows": [_window_to_dict(w) for w in output.discharge_windows],
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_diagnostics_dump(
    planner_input: PlannerInput,
    planner_output: PlannerOutput,
    apply_summary: Any | None = None,
    *,
    integration_version: str | None = None,
) -> dict[str, Any]:
    """Build a complete, safe diagnostics dump for one HSEM planning cycle.

    The returned dictionary is JSON-serialisable and suitable for:
    - Attaching to an HA ``async_get_config_entry_diagnostics`` payload.
    - Writing to disk for offline reproduction (see :func:`dump_to_json`).
    - Embedding in GitHub issue reports.

    Sensitive data (HA entity IDs, tokens, passwords) is redacted before
    the dump is returned.  The planner input section retains all numeric
    fields so the plan can be reproduced deterministically in tests via
    :func:`load_planner_input_from_dump`.

    Args:
        planner_input: The input that was fed to the planner engine.
        planner_output: The output produced by the planner engine.
        apply_summary: Optional hardware-write result from
            :class:`~custom_components.hsem.utils.inverter_verify.CycleApplySummary`.
            Entity IDs inside the summary are always redacted.
        integration_version: Optional HSEM version string to embed in the dump
            for easier triage.

    Returns:
        A JSON-safe dictionary with keys ``hsem_version``, ``planner_input``,
        ``planner_output``, and ``apply_result``.
    """
    input_dict = _planner_input_to_dict(planner_input)
    # Redact any entity-id strings that snuck into the extra dict.
    input_dict = redact_dict(input_dict)

    return {
        "hsem_version": integration_version or "unknown",
        "dump_timestamp": datetime.now().astimezone().isoformat(),
        "planner_input": input_dict,
        "planner_output": _planner_output_summary(planner_output),
        "apply_result": _apply_summary_to_dict(apply_summary),
    }


def dump_to_json(dump: dict[str, Any], *, indent: int = 2) -> str:
    """Serialise a diagnostics dump to a pretty-printed JSON string.

    Args:
        dump: A dict previously produced by :func:`build_diagnostics_dump`.
        indent: JSON indentation level.

    Returns:
        A UTF-8 JSON string.
    """
    return json.dumps(dump, indent=indent, default=str)


def load_planner_input_from_dump(dump: dict[str, Any]) -> PlannerInput:
    """Reconstruct a :class:`PlannerInput` from a diagnostics dump.

    This is the inverse of the serialisation step inside
    :func:`build_diagnostics_dump`.  It allows any dump saved during a real
    HA run to be replayed in a unit test:

    .. code-block:: python

        import json
        from custom_components.hsem.utils.diagnostics import load_planner_input_from_dump
        from custom_components.hsem.planner import run_planner

        with open("dump.json", encoding="utf-8") as fh:
            raw = json.load(fh)

        inp = load_planner_input_from_dump(raw)
        output = run_planner(inp)

    Note:
        The ``extra`` field and any redacted entity-id values are preserved as-is
        (either as ``_REDACTED`` strings or the original numeric/bool values).
        The reconstructed :class:`PlannerInput` is fully functional for replaying
        planner logic; only the HA entity strings (which the planner never uses)
        are missing.

    Args:
        dump: A dictionary previously produced by :func:`build_diagnostics_dump`.

    Returns:
        A :class:`PlannerInput` ready to be passed to :func:`run_planner`.

    Raises:
        KeyError: If ``"planner_input"`` is absent from *dump*.
        ValueError: If the serialised data is structurally invalid.
    """
    return _planner_input_from_dict(dump["planner_input"])
