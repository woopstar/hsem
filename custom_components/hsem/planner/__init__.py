"""HSEM Planner package.

This package provides a pure-Python implementation of the HSEM scheduling
engine that can be exercised in unit tests without a Home Assistant instance.

The engine is split into focused sub-modules:

- :mod:`~custom_components.hsem.planner.engine_core` — core planning flow
  (``run_planner``, candidate orchestration)
- :mod:`~custom_components.hsem.planner.engine_explanation` — explanation
  and formatting helpers (``_build_explanation``, ``_derive_windows``)

Public surface
--------------
:func:`~custom_components.hsem.planner.engine_core.run_planner`
    The top-level entry point.  Pass a fully-populated
    :class:`~custom_components.hsem.models.planner_inputs.PlannerInput` and
    receive a :class:`~custom_components.hsem.models.planner_outputs.PlannerOutput`.
"""

from custom_components.hsem.planner.engine_core import run_planner

__all__ = ["run_planner"]
