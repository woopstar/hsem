"""Declared MILP column layout and name-addressed bounds assembly.

The solver's decision vector is one flat array. Every consumer that writes into
it — the objective, the constraint matrices, and the variable bounds — has to
agree on which columns mean what. Historically each consumer recomputed those
offsets by hand, so a single arithmetic slip produced a model that solved
happily against the wrong variables.

:class:`MilpColumnLayout` makes that agreement explicit and checkable: blocks
are declared once, validated to tile the vector exactly, and thereafter
addressed **by name**. :class:`MilpBoundsBuilder` writes bounds through that
layout, so a caller cannot supply a wrong offset at all — there is no offset
parameter to get wrong.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

Bound = tuple[float | None, float | None]


@dataclass(frozen=True)
class MilpBoundsBlock:
    """One declared contiguous run of solver columns."""

    name: str
    offset: int
    width: int


class MilpColumnLayout:
    """An immutable, gap-free declaration of every solver column.

    Construction fails when the declared blocks overlap, run past the end of
    the vector, or leave any column unclaimed. A layout that constructs is
    therefore a total, unambiguous map from column index to block name.
    """

    def __init__(self, blocks: Sequence[tuple[str, int]]) -> None:
        """Build a layout from ordered ``(name, width)`` pairs."""
        declared: dict[str, MilpBoundsBlock] = {}
        offset = 0
        for name, width in blocks:
            if not name:
                raise ValueError("MILP column block name must be non-empty")
            if name in declared:
                raise ValueError(f"duplicate MILP column block: {name!r}")
            if width < 0:
                raise ValueError(
                    f"MILP column block {name!r} has negative width {width}"
                )
            declared[name] = MilpBoundsBlock(name, offset, width)
            offset += width
        self._blocks = declared
        self._column_count = offset

    @property
    def column_count(self) -> int:
        """Return the total number of declared solver columns."""
        return self._column_count

    @property
    def blocks(self) -> tuple[MilpBoundsBlock, ...]:
        """Return every declared block ordered by column offset."""
        return tuple(sorted(self._blocks.values(), key=lambda b: b.offset))

    def block(self, name: str) -> MilpBoundsBlock:
        """Return one declared block or reject an unknown name."""
        block = self._blocks.get(name)
        if block is None:
            raise ValueError(f"unknown MILP column block: {name!r}")
        return block

    def offset(self, name: str) -> int:
        """Return the first column index of one declared block."""
        return self.block(name).offset

    def width(self, name: str) -> int:
        """Return the column count of one declared block."""
        return self.block(name).width

    def has(self, name: str) -> bool:
        """Return whether *name* is declared in this layout."""
        return name in self._blocks


def build_milp_column_layout(
    m: int,
    num_evs: int,
    *,
    fuse_active: bool,
) -> MilpColumnLayout:
    """Return the canonical column layout for one MILP model.

    This is the single source of truth for the decision-vector shape. Both the
    offsets used to build the constraint matrices and the bounds assembly are
    derived from it, so the two cannot drift apart.

    Args:
        m: Number of future slots in the model.
        num_evs: Number of active EVs, each contributing a charge block and a
            single deadline-penalty column.
        fuse_active: Whether the aggregate fuse penalty block is present.

    Returns:
        A validated :class:`MilpColumnLayout`.
    """
    blocks: list[tuple[str, int]] = [
        ("battery_charge", m),
        ("battery_discharge", m),
        ("grid_import", m),
        ("grid_export", m),
        ("pv", m),
        ("primary_throughput", m),
        ("soc_max_penalty", m),
        ("soc_min_penalty", m),
        ("curtailment", m),
        ("primary_battery_export", m),
        ("battery_export_mode", m),
        ("grid_flow_mode", m),
    ]
    for ev_idx in range(num_evs):
        blocks.append((f"ev_{ev_idx}_charge", m))
        blocks.append((f"ev_{ev_idx}_target_penalty", 1))
    if fuse_active:
        blocks.append(("grid_import_penalty", m))
    return MilpColumnLayout(blocks)


class MilpBoundsBuilder:
    """Assign solver bounds by declared block name, never by call order.

    The declared *layout* is snapshotted once at construction: every column
    is checked to have exactly one owning block (no gaps, no overlap), and
    every subsequent call re-checks the live layout against that snapshot so
    a caller cannot mutate the layout out from under an in-progress build.
    """

    def __init__(self, layout: MilpColumnLayout) -> None:
        """Snapshot *layout* and preallocate one unassigned slot per column."""
        self._layout = layout
        self._column_count = layout.column_count
        self._blocks_snapshot: tuple[MilpBoundsBlock, ...] = layout.blocks
        self._blocks_by_name: dict[str, MilpBoundsBlock] = {
            block.name: block for block in self._blocks_snapshot
        }
        self._bounds: list[Bound | None] = [None] * self._column_count
        self._owners: list[str | None] = [None] * self._column_count
        self._assigned: set[str] = set()
        self._validate_layout_snapshot()

    def set(self, name: str, values: Sequence[Bound]) -> None:
        """Write one named block using exactly its declared width."""
        self._assert_layout_unchanged()
        block = self._resolve_block(name)
        if name in self._assigned:
            raise ValueError(f"duplicate MILP bounds block assignment: {name!r}")
        if len(values) != block.width:
            raise ValueError(
                f"MILP bounds block {name!r} has width {len(values)}, "
                f"expected {block.width}"
            )
        normalised = [
            self._normalise_bound(name, block.offset + index, value)
            for index, value in enumerate(values)
        ]
        self._write_block(name, block, normalised)

    def fill(self, name: str, value: Bound) -> None:
        """Write one named block filled with a single repeated bound."""
        self._assert_layout_unchanged()
        block = self._resolve_block(name)
        if name in self._assigned:
            raise ValueError(f"duplicate MILP bounds block assignment: {name!r}")
        normalised = self._normalise_bound(name, block.offset, value)
        self._write_block(name, block, [normalised] * block.width)

    def finalize(self) -> list[Bound]:
        """Return complete bounds, failing when any block was never written."""
        self._assert_layout_unchanged()
        missing = sorted(
            block.name
            for block in self._blocks_snapshot
            if block.name not in self._assigned
        )
        if missing:
            raise ValueError(f"missing MILP bounds blocks: {', '.join(missing)}")
        unassigned = [i for i, value in enumerate(self._bounds) if value is None]
        if unassigned:
            preview = ", ".join(str(i) for i in unassigned[:10])
            raise ValueError(f"unassigned MILP bounds columns: {preview}")
        return [value for value in self._bounds if value is not None]

    @property
    def blocks(self) -> tuple[MilpBoundsBlock, ...]:
        """Return the declared blocks ordered by physical column offset."""
        return self._blocks_snapshot

    def _resolve_block(self, name: str) -> MilpBoundsBlock:
        """Return one snapshotted block or reject an unknown name."""
        block = self._blocks_by_name.get(name)
        if block is None:
            raise ValueError(f"unknown MILP column block: {name!r}")
        return block

    def _write_block(
        self,
        name: str,
        block: MilpBoundsBlock,
        values: Sequence[Bound],
    ) -> None:
        """Write one already-validated block, rejecting occupied columns."""
        occupied = [
            index
            for index in range(block.offset, block.offset + block.width)
            if self._owners[index] is not None
        ]
        if occupied:
            raise ValueError(
                f"overlapping MILP bounds block {name!r} at columns "
                f"{_render_index_ranges(occupied)}"
            )
        for index, value in zip(
            range(block.offset, block.offset + block.width), values, strict=True
        ):
            self._bounds[index] = value
            self._owners[index] = name
        self._assigned.add(name)

    def _validate_layout_snapshot(self) -> None:
        """Reject a declared layout whose blocks overlap, gap, or spill over."""
        owners: list[str | None] = [None] * self._column_count
        for block in self._blocks_snapshot:
            if (
                block.offset < 0
                or block.width < 0
                or block.offset + block.width > self._column_count
            ):
                raise ValueError(
                    f"MILP bounds block {block.name!r} range "
                    f"[{block.offset}:{block.offset + block.width}] exceeds "
                    f"column count {self._column_count}"
                )
            for index in range(block.offset, block.offset + block.width):
                owner = owners[index]
                if owner is not None:
                    raise ValueError(
                        f"overlapping MILP bounds blocks {owner!r} and "
                        f"{block.name!r} at column {index}"
                    )
                owners[index] = block.name

    def _assert_layout_unchanged(self) -> None:
        """Reject use of the builder after its layout mutates post-construction."""
        if (
            self._layout.column_count != self._column_count
            or self._layout.blocks != self._blocks_snapshot
        ):
            raise ValueError("MILP column layout changed after bounds preallocation")

    @staticmethod
    def _normalise_bound(name: str, column: int, value: Bound) -> Bound:
        """Validate and normalise one lower/upper bound tuple."""
        lower = MilpBoundsBuilder._normalise_endpoint(name, column, "lower", value[0])
        upper = MilpBoundsBuilder._normalise_endpoint(name, column, "upper", value[1])
        if lower is not None and upper is not None and lower > upper:
            raise ValueError(
                f"invalid bounds in block {name!r} at column {column}: "
                f"lower {lower} exceeds upper {upper}"
            )
        return (lower, upper)

    @staticmethod
    def _normalise_endpoint(
        name: str,
        column: int,
        endpoint_name: str,
        value: float | None,
    ) -> float | None:
        """Return one finite float endpoint, retaining ``None`` as unbounded."""
        if value is None:
            return None
        if not math.isfinite(value):
            raise ValueError(
                f"invalid bounds in block {name!r} at column {column}: "
                f"{endpoint_name} endpoint {value!r} must be finite or None"
            )
        return float(value)


def _render_index_ranges(indices: Sequence[int]) -> str:
    """Render sorted column indices compactly for validation errors."""
    values = list(indices)
    if not values:
        return ""
    ranges: list[str] = []
    start = previous = values[0]
    for value in values[1:]:
        if value == previous + 1:
            previous = value
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = value
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


class MilpOffsets(NamedTuple):
    """Column offsets resolved once from a declared layout."""

    n_vars: int
    ec_off: int
    ed_off: int
    gi_off: int
    ge_off: int
    pv_off: int
    m_off: int
    s_max_off: int
    s_min_off: int
    curt_off: int
    battery_export_off: int
    export_mode_off: int
    grid_flow_mode_off: int
    ev_var_offsets: list[int]
    ev_pen_offsets: list[int]


def derive_milp_offsets(layout: MilpColumnLayout, num_evs: int) -> MilpOffsets:
    """Resolve every model offset by name from *layout*.

    Keeping this beside the layout declaration means the solver never computes
    a column index by hand, so an offset cannot drift from the declaration.
    """
    return MilpOffsets(
        n_vars=layout.column_count,
        ec_off=layout.offset("battery_charge"),
        ed_off=layout.offset("battery_discharge"),
        gi_off=layout.offset("grid_import"),
        ge_off=layout.offset("grid_export"),
        pv_off=layout.offset("pv"),
        m_off=layout.offset("primary_throughput"),
        s_max_off=layout.offset("soc_max_penalty"),
        s_min_off=layout.offset("soc_min_penalty"),
        curt_off=layout.offset("curtailment"),
        battery_export_off=layout.offset("primary_battery_export"),
        export_mode_off=layout.offset("battery_export_mode"),
        grid_flow_mode_off=layout.offset("grid_flow_mode"),
        ev_var_offsets=[layout.offset(f"ev_{i}_charge") for i in range(num_evs)],
        ev_pen_offsets=[
            layout.offset(f"ev_{i}_target_penalty") for i in range(num_evs)
        ],
    )
