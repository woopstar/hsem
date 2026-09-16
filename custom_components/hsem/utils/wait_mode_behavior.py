"""Wait-mode behaviour enumeration for HSEM.

Defines how ``batteries_wait_mode`` is interpreted when the applier writes to
the inverter: either the battery is held strictly idle, or normal household
self-consumption is allowed using only the energy above the planner's required
reserve (issue #954).

Usage
-----
>>> from custom_components.hsem.utils.wait_mode_behavior import WaitModeBehavior
>>> if cfg.batteries_wait_mode_behavior == WaitModeBehavior.SelfConsumptionWithReserve:
...     ...
"""

from enum import StrEnum


class WaitModeBehavior(StrEnum):
    """How ``batteries_wait_mode`` is executed against the hardware."""

    Strict = "strict"
    """Hold the battery idle — the house is served from the grid."""

    SelfConsumptionWithReserve = "self_consumption_with_reserve"
    """Allow household self-consumption above the planner's required reserve.

    The applier switches the slot to ``MaximizeSelfConsumption`` with a
    discharge cap derived from the surplus over the reserve, so stored energy
    beyond what later slots need can serve live house load (issue #954).
    """


WAIT_MODE_BEHAVIOR_VALUES: tuple[str, ...] = tuple(
    member.value for member in WaitModeBehavior
)
"""Every accepted wait-mode behaviour value, in declaration order.

Import this for flow validation rather than restating the values — the flow's
accepted set and the applier's comparison must never diverge, or the opt-in
self-consumption behaviour silently stops taking effect.
"""

DEFAULT_WAIT_MODE_BEHAVIOR: str = WaitModeBehavior.Strict.value
"""Default behaviour: hold the battery idle.

Chosen as the default because it preserves the pre-#954 behaviour, so an
existing config entry that has never seen this option keeps working as before.
"""
