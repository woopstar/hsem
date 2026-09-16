"""Working mode enumerations for Huawei Luna2000 inverters.

Defines the three inverter operating modes that HSEM can switch between, plus
the excess-PV routing options HSEM writes alongside them.
"""

from enum import Enum, StrEnum


class WorkingModes(Enum):
    """Huawei Luna2000 inverter working modes."""

    TimeOfUse = "time_of_use_luna2000"
    """Time-of-Use mode: battery charges/discharges according to a TOU schedule."""

    MaximizeSelfConsumption = "maximise_self_consumption"
    """Maximise self-consumption: battery prioritises powering the home over export."""

    FullyFedToGrid = "fully_fed_to_grid"
    """Fully fed to grid: all solar production is exported to the grid."""


class ExcessPvUseInTou(StrEnum):
    """Where surplus PV goes while the inverter is in Time-of-Use mode.

    These are the option values of the Huawei
    ``batteries_excess_pv_energy_use_in_tou`` select entity.  The valid set is
    dictated by the upstream Huawei Solar integration, not by HSEM, so the
    values must not be renamed here.

    Note this ``"charge"`` is unrelated to the ``"charge"`` action label
    returned by ``utils.prediction_tracker._action_label`` and
    ``planner.window_hysteresis`` — those describe a battery direction, not an
    inverter setting, and the two must not be conflated.
    """

    Charge = "charge"
    """Route surplus PV into the battery."""

    FedToGrid = "fed_to_grid"
    """Export surplus PV to the grid instead of storing it."""
