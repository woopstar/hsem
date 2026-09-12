"""Diagnostics dump utilities for HSEM.

Single responsibility: produce a safe, JSON-serialisable snapshot of one
HSEM planning cycle that can be:

- Attached to a Home Assistant diagnostics report (``async_get_config_entry_diagnostics``).
- Shared in GitHub issue reports without leaking credentials.

Redaction
---------
Any field whose name or value looks like an HA entity-id (``domain.name``),
token, password, or other sensitive identifier is either omitted or replaced
with ``"**REDACTED**"`` before the dump is returned.

Specifically, Huawei Solar entity IDs listed in the live state (e.g.
``sensor.batteries_state_of_capacity``) and config entry entity references are
replaced so that the dump does not expose the user's HA entity namespace.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, cast

import homeassistant.util.dt as dt_util
from homeassistant.const import STATE_UNKNOWN

from custom_components.hsem.models.planner_input import PlannerInput
from custom_components.hsem.models.planner_output import PlannerOutput

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


def _serialise_value(value: Any) -> Any:
    """Recursively make a value JSON-safe.

    Handles ``datetime`` / ``date`` objects and plain containers.  Non-serialisable
    objects are replaced with their repr string so the dump never crashes a
    service response.

    Args:
        value: Any value from a dataclass ``asdict()`` result.

    Returns:
        A JSON-safe representation of *value*.
    """
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _serialise_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_serialise_value(item) for item in value]
    if isinstance(value, tuple):
        return [_serialise_value(item) for item in value]
    return value


def _planner_input_to_dict(inp: PlannerInput) -> dict[str, Any]:
    """Convert a :class:`PlannerInput` to a JSON-safe dictionary.

    Datetime/date fields are serialised to ISO-8601 strings.  The
    ``solar_corrector`` object is replaced with a placeholder because it is
    not serialisable and is not needed to reproduce planner logic offline.

    Args:
        inp: The planner input to serialise.

    Returns:
        A JSON-serialisable dictionary.
    """
    raw = asdict(inp)

    # The solar corrector is a runtime object; it is not serialisable and is
    # not needed to reproduce planner logic offline, so replace it with None.
    raw["solar_corrector"] = None

    return cast(dict[str, Any], _serialise_value(raw))


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
        "solcast_pv_estimate_kwh": round(slot.solcast_pv_estimate_kwh, 3),
        "avg_house_consumption_kwh": round(slot.avg_house_consumption_kwh, 3),
        "estimated_net_consumption_kwh": round(slot.estimated_net_consumption_kwh, 3),
        "estimated_cost_currency": round(slot.estimated_cost_currency, 4),
        "estimated_battery_soc_pct": round(slot.estimated_battery_soc_pct, 1),
        "batteries_charged_kwh": round(slot.batteries_charged_kwh, 3),
        "batteries_discharged_kwh": round(slot.batteries_discharged_kwh, 3),
        "grid_import_kwh": round(slot.grid_import_kwh, 3),
        "grid_export_kwh": round(slot.grid_export_kwh, 3),
        "recommendation": slot.recommendation,
        # EV load semantics (issue #404):
        #   ev_planned_load_kwh     — extra load injected into net consumption
        #   ev_accounted_load_kwh   — load already in house consumption sensor
        #   ev_total_planned_load_kwh — total EV load regardless of base_load_includes_ev
        "ev_planned_load_kwh": round(slot.ev_planned_load_kwh, 3),
        "ev_accounted_load_kwh": round(slot.ev_accounted_load_kwh, 3),
        "ev_total_planned_load_kwh": round(slot.ev_total_planned_load_kwh, 3),
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
    - Embedding in GitHub issue reports.

    Sensitive data (HA entity IDs, tokens, passwords) is redacted before
    the dump is returned.

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
        "hsem_version": integration_version or STATE_UNKNOWN,
        "dump_timestamp": dt_util.now().isoformat(),
        "planner_input": input_dict,
        "planner_output": _planner_output_summary(planner_output),
        "apply_result": _apply_summary_to_dict(apply_summary),
    }
