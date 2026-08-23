"""Structural regressions for the declared MILP column layout.

The point of the declared layout is that a consumer addresses solver columns by
**name**, so a wrong offset is not merely detected — it cannot be expressed.
These tests pin both halves: the layout validates its own declaration, and the
bounds builder refuses anything the layout does not sanction.
"""

from __future__ import annotations

import pytest

from custom_components.hsem.planner.milp._layout import (
    MilpBoundsBuilder,
    MilpColumnLayout,
    build_milp_column_layout,
)

# ---------------------------------------------------------------------------
# Layout declaration
# ---------------------------------------------------------------------------


def test_layout_tiles_the_vector_without_gaps_or_overlap() -> None:
    """Declared blocks are laid out contiguously in declaration order."""
    layout = MilpColumnLayout([("left", 2), ("middle", 1), ("right", 3)])

    assert layout.column_count == 6
    assert layout.offset("left") == 0
    assert layout.offset("middle") == 2
    assert layout.offset("right") == 3
    assert layout.width("right") == 3
    assert [block.name for block in layout.blocks] == ["left", "middle", "right"]


def test_layout_rejects_duplicate_block_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        MilpColumnLayout([("charge", 2), ("charge", 2)])


def test_layout_rejects_negative_width() -> None:
    with pytest.raises(ValueError, match="negative width"):
        MilpColumnLayout([("charge", -1)])


def test_layout_rejects_empty_block_name() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        MilpColumnLayout([("", 1)])


def test_unknown_block_name_is_rejected() -> None:
    layout = MilpColumnLayout([("charge", 1)])
    with pytest.raises(ValueError, match="unknown MILP column block"):
        layout.offset("discharge")


# ---------------------------------------------------------------------------
# Canonical model layout
# ---------------------------------------------------------------------------


def test_canonical_layout_column_count_matches_declared_blocks() -> None:
    """The canonical layout accounts for every column it claims."""
    m, num_evs = 8, 2
    layout = build_milp_column_layout(m, num_evs, fuse_active=True)

    # 12 base blocks of width m, per EV (m charge + 1 penalty), plus fuse
    # penalty of width m.
    expected = 12 * m + num_evs * (m + 1) + m
    assert layout.column_count == expected
    assert sum(block.width for block in layout.blocks) == expected


def test_canonical_layout_omits_fuse_block_when_inactive() -> None:
    layout = build_milp_column_layout(4, 0, fuse_active=False)
    assert not layout.has("grid_import_penalty")
    assert layout.column_count == 12 * 4


def test_canonical_layout_blocks_are_strictly_ordered() -> None:
    """Every block starts exactly where the previous one ended."""
    layout = build_milp_column_layout(6, 3, fuse_active=True)
    cursor = 0
    for block in layout.blocks:
        assert block.offset == cursor, f"gap or overlap before {block.name}"
        cursor += block.width
    assert cursor == layout.column_count


def test_canonical_layout_separates_each_ev() -> None:
    """Per-EV blocks never share columns."""
    layout = build_milp_column_layout(4, 3, fuse_active=False)
    seen: set[int] = set()
    for ev_idx in range(3):
        for name in (f"ev_{ev_idx}_charge", f"ev_{ev_idx}_target_penalty"):
            block = layout.block(name)
            columns = set(range(block.offset, block.offset + block.width))
            assert not (columns & seen), f"{name} overlaps an earlier EV block"
            seen |= columns


# ---------------------------------------------------------------------------
# Bounds assembly through the layout
# ---------------------------------------------------------------------------


def _small_layout() -> MilpColumnLayout:
    return MilpColumnLayout([("left", 2), ("right", 2)])


def test_bounds_finalize_in_physical_column_order() -> None:
    """Write order does not affect physical column order."""
    builder = MilpBoundsBuilder(_small_layout())
    builder.set("right", [(0.0, 2.0), (0.0, 3.0)])
    builder.set("left", [(0.0, 0.0), (1.0, None)])

    assert builder.finalize() == [
        (0.0, 0.0),
        (1.0, None),
        (0.0, 2.0),
        (0.0, 3.0),
    ]
    assert [block.name for block in builder.blocks] == ["left", "right"]


def test_duplicate_block_assignment_fails_fast() -> None:
    builder = MilpBoundsBuilder(_small_layout())
    builder.fill("left", (0.0, 1.0))
    with pytest.raises(ValueError, match="duplicate"):
        builder.fill("left", (0.0, 1.0))


def test_unknown_block_cannot_be_written() -> None:
    """A name the layout never declared is rejected outright."""
    builder = MilpBoundsBuilder(_small_layout())
    with pytest.raises(ValueError, match="unknown MILP column block"):
        builder.fill("nonexistent", (0.0, 1.0))


def test_wrong_width_fails_fast() -> None:
    """A block must be written with exactly its declared width."""
    builder = MilpBoundsBuilder(_small_layout())
    with pytest.raises(ValueError, match="expected 2"):
        builder.set("left", [(0.0, 1.0)])


def test_missing_block_fails_finalize() -> None:
    builder = MilpBoundsBuilder(_small_layout())
    builder.fill("left", (0.0, 1.0))
    with pytest.raises(ValueError, match="missing MILP bounds blocks: right"):
        builder.finalize()


def test_invalid_bound_fails_fast() -> None:
    builder = MilpBoundsBuilder(_small_layout())
    with pytest.raises(ValueError, match="invalid bounds"):
        builder.fill("left", (2.0, 1.0))


def test_zero_width_block_is_satisfied_by_assignment() -> None:
    """A declared but empty block still has to be written explicitly."""
    builder = MilpBoundsBuilder(MilpColumnLayout([("empty", 0), ("real", 1)]))
    builder.fill("real", (0.0, 1.0))
    with pytest.raises(ValueError, match="missing MILP bounds blocks: empty"):
        builder.finalize()
    builder.set("empty", [])
    assert builder.finalize() == [(0.0, 1.0)]
