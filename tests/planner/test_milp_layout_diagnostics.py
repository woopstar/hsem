"""Tests for the MILP bounds layout's self-defence and error rendering.

``test_milp_bounds_layout.py`` covers the declared-layout contract. These tests
cover the builder's defences against a layout it was handed rather than one it
built — a stub layout with overlapping or out-of-range blocks — and the compact
column-range renderer those errors use.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from custom_components.hsem.planner.milp._layout import (
    MilpBoundsBlock,
    MilpBoundsBuilder,
    MilpColumnLayout,
    _render_index_ranges,
)


class _StubLayout:
    """A layout stand-in that can declare blocks a real layout would reject."""

    def __init__(self, column_count: int, blocks: Sequence[MilpBoundsBlock]) -> None:
        self._column_count = column_count
        self._blocks = tuple(blocks)

    @property
    def column_count(self) -> int:
        return self._column_count

    @property
    def blocks(self) -> tuple[MilpBoundsBlock, ...]:
        return self._blocks


class TestRenderIndexRanges:
    """Column indices are rendered compactly for validation errors."""

    @pytest.mark.parametrize(
        ("indices", "expected"),
        [
            pytest.param([], "", id="empty"),
            pytest.param([3], "3", id="single"),
            pytest.param([0, 1, 2], "0-2", id="one_run"),
            pytest.param([0, 2, 4], "0,2,4", id="all_isolated"),
            pytest.param([0, 1, 2, 5, 7, 8], "0-2,5,7-8", id="mixed_runs"),
            pytest.param([9, 10], "9-10", id="pair"),
        ],
    )
    def test_renders_runs_and_singletons(
        self, indices: list[int], expected: str
    ) -> None:
        """Consecutive indices collapse to a range; isolated ones stay single."""
        assert _render_index_ranges(indices) == expected


class TestLayoutSnapshotDefence:
    """The builder validates whatever layout it is given, at construction."""

    def test_overlapping_blocks_are_rejected_with_both_names(self) -> None:
        """Two blocks claiming one column would make bounds ambiguous."""
        layout = _StubLayout(
            4,
            [
                MilpBoundsBlock("left", 0, 3),
                MilpBoundsBlock("right", 2, 2),
            ],
        )

        with pytest.raises(ValueError, match="overlapping MILP bounds blocks"):
            MilpBoundsBuilder(layout)  # type: ignore[arg-type]  # deliberately malformed layout

    def test_block_running_past_the_vector_is_rejected(self) -> None:
        """A block that spills over would write outside the vector."""
        layout = _StubLayout(2, [MilpBoundsBlock("wide", 0, 5)])

        with pytest.raises(ValueError, match="exceeds column count"):
            MilpBoundsBuilder(layout)  # type: ignore[arg-type]  # deliberately malformed layout

    def test_negative_offset_is_rejected(self) -> None:
        """A negative offset would index from the end of the vector."""
        layout = _StubLayout(2, [MilpBoundsBlock("shifted", -1, 2)])

        with pytest.raises(ValueError, match="exceeds column count"):
            MilpBoundsBuilder(layout)  # type: ignore[arg-type]  # deliberately malformed layout


class TestDuplicateAssignment:
    """A block may only be written once per build."""

    def test_set_refuses_a_second_write(self) -> None:
        """``set`` is as strict about duplicates as ``fill``."""
        builder = MilpBoundsBuilder(MilpColumnLayout([("left", 2), ("right", 1)]))
        builder.set("left", [(0.0, 1.0), (0.0, 2.0)])

        with pytest.raises(ValueError, match="duplicate"):
            builder.set("left", [(0.0, 1.0), (0.0, 2.0)])

    def test_set_and_fill_share_the_same_assignment_ledger(self) -> None:
        """A block written with ``fill`` cannot then be written with ``set``."""
        builder = MilpBoundsBuilder(MilpColumnLayout([("left", 2), ("right", 1)]))
        builder.fill("left", (0.0, 1.0))

        with pytest.raises(ValueError, match="duplicate"):
            builder.set("left", [(0.0, 1.0), (0.0, 2.0)])
