"""Reflection test guarding the tracker-persistence registry (issue #890).

``vulture`` cannot catch a tracker class that implements a fully-tested
``save_history()`` method with zero production call sites when another
tracker class happens to share that method name -- it matches unused code
by name only, not by class (see :mod:`coordinator_persistence` for the
full story, and issue #888 for the bug this blind spot let through).

This test walks the type annotations declared on
:class:`~custom_components.hsem.coordinator_state.CoordinatorSharedState`,
finds every field whose type exposes an ``async def save_history(self) ->
bool`` method, and asserts it has a corresponding entry in
:data:`~custom_components.hsem.coordinator_persistence.TRACKER_REGISTRY`.
Adding a new persistable tracker field without wiring it into the
registry must fail this test immediately.
"""

from __future__ import annotations

import types
import typing

from custom_components.hsem import coordinator_state
from custom_components.hsem.coordinator_persistence import TRACKER_REGISTRY
from custom_components.hsem.ml.consumption_predictor import ConsumptionPredictor


def _unwrap_union(annotation: object) -> list[object]:
    """Return the non-``None`` member types of a (possibly) union annotation."""
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        return [a for a in typing.get_args(annotation) if a is not type(None)]
    return [annotation]


def _resolved_field_types() -> dict[str, list[object]]:
    """Resolve every field annotation on ``CoordinatorSharedState``.

    ``coordinator_state.py`` uses ``from __future__ import annotations``,
    so annotations are strings until resolved with ``get_type_hints``.
    ``ConsumptionPredictor`` is only imported under ``TYPE_CHECKING`` in
    that module, so it is supplied explicitly via ``localns``.
    """
    hints = typing.get_type_hints(
        coordinator_state.CoordinatorSharedState,
        globalns=dict(vars(coordinator_state)),
        localns={"ConsumptionPredictor": ConsumptionPredictor},
    )
    return {name: _unwrap_union(annotation) for name, annotation in hints.items()}


def _fields_exposing_save_history() -> set[str]:
    """Return every field name whose type defines ``save_history``."""
    persistable: set[str] = set()
    for name, types_ in _resolved_field_types().items():
        if any(hasattr(t, "save_history") for t in types_):
            persistable.add(name)
    return persistable


def test_registry_covers_every_persistable_tracker() -> None:
    """Every ``save_history``-exposing tracker field must be registered.

    This is the regression guard for issue #890: if a future tracker
    field defines ``save_history`` but nobody adds a
    ``TRACKER_REGISTRY`` entry for it, this test fails immediately
    instead of silently shipping an orphaned persistence method the way
    ``SavingsTracker.save_history()`` did in issue #888.
    """
    persistable_fields = _fields_exposing_save_history()
    registered = set(TRACKER_REGISTRY.keys())

    missing = persistable_fields - registered
    assert not missing, (
        "Coordinator field(s) expose save_history() but are missing from "
        f"TRACKER_REGISTRY in coordinator_persistence.py: {sorted(missing)}"
    )

    stale = registered - persistable_fields
    assert not stale, (
        "TRACKER_REGISTRY references field(s) that no longer expose "
        f"save_history() (or no longer exist): {sorted(stale)}"
    )


def test_registry_entries_are_real_coordinator_fields() -> None:
    """Every registry key must match a real field on CoordinatorSharedState."""
    declared_fields = set(_resolved_field_types().keys())
    for name in TRACKER_REGISTRY:
        assert name in declared_fields, (
            f"TRACKER_REGISTRY key {name!r} is not a field on "
            "CoordinatorSharedState (coordinator_state.py)"
        )


def test_known_persistable_trackers_are_registered() -> None:
    """Sanity check pinning the trackers known to expose save_history() today."""
    assert _fields_exposing_save_history() == {
        "_savings_tracker",
        "_financial_tracker",
        "_prediction_tracker",
    }
