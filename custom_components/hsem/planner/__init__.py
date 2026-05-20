"""HSEM Planner package.

This package provides a pure-Python implementation of the HSEM scheduling
engine that can be exercised in unit tests without a Home Assistant instance.

The engine has been refactored into focused sub-modules:

- :mod:`~custom_components.hsem.planner.engine_core` — core planning flow
  (:func:`run_planner`, candidate orchestration)
- :mod:`~custom_components.hsem.planner.engine_explanation` — explanation
  and formatting helpers (``_build_explanation``, ``_derive_windows``)
- :mod:`~custom_components.hsem.planner.engine_hardware` — hardware write
  logic (stub; inverter dispatch lives in coordinator)
- :mod:`~custom_components.hsem.planner.engine` — facade that re-exports
  public symbols for backward compatibility

Public surface
--------------
:func:`~custom_components.hsem.planner.engine.run_planner`
    The top-level entry point.  Pass a fully-populated
    :class:`~custom_components.hsem.models.planner_inputs.PlannerInput` and
    receive a :class:`~custom_components.hsem.models.planner_outputs.PlannerOutput`.
"""

from custom_components.hsem.planner.engine import run_planner

__all__ = ["run_planner"]
