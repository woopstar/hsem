"""HSEM Planner package.

This package provides a pure-Python implementation of the HSEM scheduling
engine that can be exercised in unit tests without a Home Assistant instance.

Public surface
--------------
:func:`~custom_components.hsem.planner.engine.run_planner`
    The top-level entry point.  Pass a fully-populated
    :class:`~custom_components.hsem.models.planner_inputs.PlannerInput` and
    receive a :class:`~custom_components.hsem.models.planner_outputs.PlannerOutput`.
"""

from custom_components.hsem.planner.engine import run_planner

__all__ = ["run_planner"]
