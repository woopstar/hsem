"""Structural regressions for named MILP bounds assembly."""

import pytest

from custom_components.hsem.planner.milp._layout import MilpBoundsBuilder


def test_named_bounds_finalize_in_physical_column_order() -> None:
    builder = MilpBoundsBuilder(4)
    builder.set("right", 2, [(0.0, 2.0), (0.0, 3.0)])
    builder.set("left", 0, [(0.0, 0.0), (1.0, None)])

    assert builder.finalize() == [
        (0.0, 0.0),
        (1.0, None),
        (0.0, 2.0),
        (0.0, 3.0),
    ]
    assert [block.name for block in builder.blocks] == ["left", "right"]


def test_duplicate_block_name_fails_fast() -> None:
    builder = MilpBoundsBuilder(2)
    builder.fill("charge", 0, 1, (0.0, 1.0))
    with pytest.raises(ValueError, match="duplicate"):
        builder.fill("charge", 1, 1, (0.0, 1.0))


def test_overlapping_block_fails_fast() -> None:
    builder = MilpBoundsBuilder(3)
    builder.fill("first", 0, 2, (0.0, 1.0))
    with pytest.raises(ValueError, match="overlaps"):
        builder.fill("second", 1, 2, (0.0, 1.0))


def test_wrong_width_or_offset_fails_fast() -> None:
    builder = MilpBoundsBuilder(2)
    with pytest.raises(ValueError, match="exceeds"):
        builder.fill("too_wide", 1, 2, (0.0, 1.0))


def test_missing_column_fails_finalize() -> None:
    builder = MilpBoundsBuilder(3)
    builder.fill("partial", 0, 2, (0.0, 1.0))
    with pytest.raises(ValueError, match="unassigned"):
        builder.finalize()


def test_invalid_bound_fails_fast() -> None:
    builder = MilpBoundsBuilder(1)
    with pytest.raises(ValueError, match="invalid bounds"):
        builder.fill("invalid", 0, 1, (2.0, 1.0))
