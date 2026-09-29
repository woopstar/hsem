"""Entity IDs referenced by the bundled dashboard and the docs must exist.

Every HSEM entity sets its ``entity_id`` explicitly from a
``utils/sensornames`` helper, so those helpers are the single source of truth.
The docs had drifted from them (for example ``sensor.hsem_plan_explanation``
for ``sensor.hsem_plan_explanation_sensor``), and the bundled dashboard's
Savings Tracker tile pointed at a non-existent ``sensor.hsem_savings_tracker``.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import custom_components.hsem.utils.sensornames as sensornames

_REPO = Path(__file__).resolve().parent.parent
_DASHBOARD = _REPO / "custom_components" / "hsem" / "dashboards" / "dashboard_en.yaml"
_DOCS = sorted([*(_REPO / "docs").rglob("*.md"), _REPO / "README.md"])

_HSEM_ID = re.compile(
    r"\b(?:sensor|switch|select|number|time|button|binary_sensor)\.hsem_[a-z0-9_]+"
)
# Per-hour consumption sensors are built from hour (and averaging) parameters.
_PER_HOUR = re.compile(
    r"sensor\.hsem_house_consumption_"
    r"(?:energy_avg|energy_integral|energy|power)_\d{2}_\d{2}(?:_[a-z0-9]+)*"
)


def _registered_entity_ids() -> set[str]:
    """Return every entity ID the ``sensornames`` helpers produce.

    Helpers with required parameters (the per-hour consumption families) are
    called with zeros, so their IDs still serve as prefix examples.  Helpers
    taking ``charger_index`` are also called for the second charger.
    """
    ids: set[str] = set()
    for info in pkgutil.iter_modules(sensornames.__path__):
        module = importlib.import_module(f"{sensornames.__name__}.{info.name}")
        for name, fn in inspect.getmembers(module, inspect.isfunction):
            if not name.endswith("_entity_id") or fn.__module__ != module.__name__:
                continue
            params = inspect.signature(fn).parameters
            required = [
                p for p in params.values() if p.default is inspect.Parameter.empty
            ]
            ids.add(fn(*([0] * len(required))))
            if "charger_index" in params:
                ids.add(fn(charger_index=2))
    return ids


def _unknown_ids(text: str, registered: set[str]) -> set[str]:
    """Return HSEM entity IDs in *text* that no helper produces.

    A trailing ``_`` marks a prefix pattern (``sensor.hsem_ev_…``); it is
    accepted when some registered ID starts with it.
    """
    return {
        entity_id
        for entity_id in _HSEM_ID.findall(text)
        if entity_id not in registered
        and not _PER_HOUR.fullmatch(entity_id)
        and not (
            entity_id.endswith("_") and any(r.startswith(entity_id) for r in registered)
        )
    }


def test_registry_enumeration_is_not_vacuous() -> None:
    """The helper walk finds real IDs, including second-charger variants."""
    registered = _registered_entity_ids()

    assert "sensor.hsem_workingmode_sensor" in registered
    assert "sensor.hsem_effective_discharge_floor_sensor" in registered
    assert "sensor.hsem_ocpp_charger_status_sensor_second" in registered
    assert "switch.hsem_dynamic_discharge_floor" in registered
    assert len(registered) > 50


def test_unknown_ids_flags_a_missing_suffix() -> None:
    """``sensor.hsem_plan_explanation`` (no ``_sensor``) is reported."""
    text = "See `sensor.hsem_plan_explanation` and `sensor.hsem_ev_*`."

    assert _unknown_ids(text, _registered_entity_ids()) == {
        "sensor.hsem_plan_explanation"
    }


def test_bundled_dashboard_references_only_registered_entities() -> None:
    """Every HSEM tile in the shipped dashboard points at a real entity."""
    unknown = _unknown_ids(
        _DASHBOARD.read_text(encoding="utf-8"), _registered_entity_ids()
    )

    assert unknown == set()


def test_docs_reference_only_registered_entities() -> None:
    """The docs only name HSEM entity IDs that the integration registers."""
    registered = _registered_entity_ids()
    unknown = {
        str(path.relative_to(_REPO)): ids
        for path in _DOCS
        if (ids := _unknown_ids(path.read_text(encoding="utf-8"), registered))
    }

    assert unknown == {}
