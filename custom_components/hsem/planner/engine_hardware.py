"""Hardware write logic for the HSEM planner.

Single responsibility: inverter command dispatch, read-only mode guards, and
Huawei Solar API interaction helpers.

Currently this is a stub — all hardware-write logic lives in the
coordinator and service layers.  As the planner evolves, inverter dispatch
and read-only guard helpers should move here so they can be unit-tested
independently.

**No Home Assistant types are imported here.**  This makes the module
directly testable with plain ``pytest`` without a running HA instance.
"""

from __future__ import annotations
