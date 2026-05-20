"""Facade module for backward compatibility.

Re-exports public symbols from the refactored engine sub-modules:

- :func:`run_planner` from :mod:`engine_core`
- :func:`_parse_now` from :mod:`engine_core` (used by tests)

**No Home Assistant types are imported here.**  This makes the engine
directly testable with plain ``pytest`` without a running HA instance.
"""

from custom_components.hsem.planner.engine_core import (  # noqa: F401  # imported by tests
    _parse_now,
    run_planner,
)

__all__ = ["run_planner"]
