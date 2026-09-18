"""Tests for ``validate_incumbent`` — the MILP incumbent safety check.

When the solver hits its time limit HSEM may still decode the incumbent, so
that vector is re-checked against the complete model first. Every rejection
reason matters: an accepted infeasible vector would be decoded into real
inverter and charger commands.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from custom_components.hsem.planner.milp._incumbent import (
    IncumbentValidation,
    validate_incumbent,
)

_N_VARS = 3


def _model(**overrides: Any) -> dict[str, Any]:
    """Return a tiny feasible model: ``x0 + x1 = 1``, ``x2 <= 1``, all in [0, 1]."""
    model: dict[str, Any] = {
        "n_vars": _N_VARS,
        "slot_count": 4,
        "future_idx": [0, 1],
        "m": 2,
        "variable_blocks": {"battery": (0, 2), "ev": (2, 1)},
        "a_eq": [[1.0, 1.0, 0.0]],
        "b_eq": [1.0],
        "a_ub": [[0.0, 0.0, 1.0]],
        "b_ub": [1.0],
        "bounds": [(0.0, 1.0)] * _N_VARS,
        "integrality": None,
    }
    model.update(overrides)
    return model


def _validate(x: Any, **overrides: Any) -> IncumbentValidation:
    """Validate *x* against the tiny model with *overrides* applied."""
    return validate_incumbent(x, **_model(**overrides))


class TestAcceptedIncumbent:
    """A vector satisfying every part of the model is accepted."""

    def test_feasible_vector_is_valid(self) -> None:
        """Bounds, equalities, and inequalities all hold."""
        result = _validate([0.4, 0.6, 0.5])

        assert result.valid is True
        assert result.reason == "feasible"
        assert result.max_equality_residual == pytest.approx(0.0)
        assert result.max_inequality_violation == pytest.approx(0.0)
        assert result.max_bound_violation == pytest.approx(0.0)

    def test_violations_within_tolerance_are_accepted(self) -> None:
        """Solver round-off below the feasibility tolerance is not a failure."""
        result = _validate([0.4, 0.6 + 1e-9, 0.5])

        assert result.valid is True

    def test_integer_variables_are_accepted_at_whole_values(self) -> None:
        """An integer amp command at a whole value passes."""
        result = _validate([0.4, 0.6, 1.0], integrality=[0, 0, 1])

        assert result.valid is True

    def test_semi_continuous_variable_may_sit_at_zero(self) -> None:
        """A switched-off EV amp command is valid below its own lower bound."""
        result = _validate(
            [0.4, 0.6, 0.0],
            bounds=[(0.0, 1.0), (0.0, 1.0), (0.5, 1.0)],
            integrality=[0, 0, 2],
        )

        assert result.valid is True


class TestRejectedVectorShape:
    """A vector that does not match the model is refused outright."""

    def test_missing_vector(self) -> None:
        """No incumbent means nothing to decode."""
        assert _validate(None).reason == "missing_solution_vector"

    def test_unconvertible_vector(self) -> None:
        """A vector that is not numeric cannot be checked."""
        assert _validate(["a", "b", "c"]).reason == "invalid_solution_vector"

    def test_multi_dimensional_vector(self) -> None:
        """The decision vector must be one-dimensional."""
        assert (
            _validate([[0.4, 0.6, 0.5]]).reason == "solution_vector_not_one_dimensional"
        )

    def test_wrong_length_vector(self) -> None:
        """A length mismatch names both lengths for diagnosis."""
        assert _validate([0.4, 0.6]).reason == ("solution_vector_length_2_expected_3")

    @pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
    def test_non_finite_values(self, bad: float) -> None:
        """A non-finite entry would decode into a nonsense command."""
        assert _validate([0.4, bad, 0.5]).reason == "solution_vector_not_finite"


class TestRejectedHorizon:
    """The future-slot index list must describe the solved horizon."""

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            pytest.param(
                {"m": 0, "future_idx": []},
                "future_horizon_length_mismatch",
                id="empty_horizon",
            ),
            pytest.param(
                {"m": 3}, "future_horizon_length_mismatch", id="length_mismatch"
            ),
            pytest.param(
                {"future_idx": [0, 9]},
                "future_horizon_index_out_of_range",
                id="index_beyond_slots",
            ),
            pytest.param(
                {"future_idx": [-1, 1]},
                "future_horizon_index_out_of_range",
                id="negative_index",
            ),
            pytest.param(
                {"future_idx": [1, 1]},
                "future_horizon_not_strictly_increasing",
                id="repeated_index",
            ),
            pytest.param(
                {"future_idx": [1, 0]},
                "future_horizon_not_strictly_increasing",
                id="descending_index",
            ),
        ],
    )
    def test_invalid_horizon_is_refused(
        self, overrides: dict[str, Any], reason: str
    ) -> None:
        """A horizon that cannot be mapped back to slots is refused."""
        assert _validate([0.4, 0.6, 0.5], **overrides).reason == reason


class TestRejectedLayout:
    """Variable blocks and bounds must fit the declared vector."""

    def test_block_outside_the_vector(self) -> None:
        """A block reaching past the vector would decode foreign memory."""
        result = _validate(
            [0.4, 0.6, 0.5], variable_blocks={"battery": (0, 2), "ev": (2, 5)}
        )

        assert result.reason == "variable_block_ev_out_of_range"

    def test_negative_block_offset(self) -> None:
        """A negative offset is rejected by name."""
        result = _validate([0.4, 0.6, 0.5], variable_blocks={"battery": (-1, 2)})

        assert result.reason == "variable_block_battery_out_of_range"

    def test_bounds_length_mismatch(self) -> None:
        """One bound per variable is required."""
        assert _validate([0.4, 0.6, 0.5], bounds=[(0.0, 1.0)]).reason == (
            "bounds_length_1_expected_3"
        )

    def test_integrality_shape_mismatch(self) -> None:
        """One integrality flag per variable is required."""
        assert _validate([0.4, 0.6, 0.5], integrality=[0, 1]).reason == (
            "integrality_vector_shape_mismatch"
        )


class TestRejectedConstraints:
    """Bound, equality, inequality, and integrality violations are reported."""

    def test_lower_bound_violation(self) -> None:
        """A value below its lower bound is reported with its magnitude."""
        result = _validate([-0.5, 1.5, 0.5])

        assert result.valid is False
        assert result.reason == "bound_violation"
        assert result.max_bound_violation == pytest.approx(0.5)

    def test_upper_bound_violation(self) -> None:
        """A value above its upper bound is reported with its magnitude."""
        result = _validate([0.4, 0.6, 1.5])

        assert result.reason == "bound_violation"
        assert result.max_bound_violation == pytest.approx(0.5)

    def test_equality_violation(self) -> None:
        """An unbalanced energy row is refused with its residual."""
        result = _validate([0.1, 0.1, 0.5])

        assert result.reason == "equality_constraint_violation"
        assert result.max_equality_residual == pytest.approx(0.8)

    def test_inequality_violation(self) -> None:
        """An exceeded limit row is refused with its violation."""
        result = _validate([0.4, 0.6, 2.0], bounds=[(0.0, 1.0), (0.0, 1.0), (0.0, 5.0)])

        assert result.reason == "inequality_constraint_violation"
        assert result.max_inequality_violation == pytest.approx(1.0)

    def test_integrality_violation(self) -> None:
        """A fractional integer command is refused with its distance."""
        result = _validate([0.4, 0.6, 0.5], integrality=[0, 0, 1])

        assert result.reason == "integrality_violation"
        assert result.max_integrality_violation == pytest.approx(0.5)

    def test_semi_continuous_values_are_not_rounded(self) -> None:
        """Type 2 is continuous above zero, so a fraction is legitimate."""
        result = _validate(
            [0.4, 0.6, 0.5],
            bounds=[(0.0, 1.0), (0.0, 1.0), (0.25, 1.0)],
            integrality=[0, 0, 2],
        )

        assert result.valid is True


class TestRejectedMatrices:
    """A matrix that does not match the vector cannot be evaluated."""

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            pytest.param(
                {"a_eq": [[1.0, 1.0]]},
                "equality_matrix_shape_mismatch",
                id="eq_columns",
            ),
            pytest.param(
                {"b_eq": [1.0, 2.0]}, "equality_rhs_shape_mismatch", id="eq_rhs"
            ),
            pytest.param(
                {"a_ub": [[1.0, 1.0]]},
                "inequality_matrix_shape_mismatch",
                id="ub_columns",
            ),
            pytest.param(
                {"b_ub": [1.0, 2.0]}, "inequality_rhs_shape_mismatch", id="ub_rhs"
            ),
        ],
    )
    def test_shape_mismatches_are_refused(
        self, overrides: dict[str, Any], reason: str
    ) -> None:
        """Each matrix/RHS mismatch is named distinctly."""
        assert _validate([0.4, 0.6, 0.5], **overrides).reason == reason
