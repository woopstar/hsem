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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

Bound = tuple[float, float | None]


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
    """Assign solver bounds by declared block name, never by call order."""

    def __init__(self, layout: MilpColumnLayout) -> None:
        """Preallocate one unassigned slot per column declared by *layout*."""
        self._layout = layout
        self._bounds: list[Bound | None] = [None] * layout.column_count
        self._assigned: set[str] = set()

    def set(self, name: str, values: Sequence[Bound]) -> None:
        """Write one named block using exactly its declared width."""
        block = self._layout.block(name)
        if name in self._assigned:
            raise ValueError(f"duplicate MILP bounds block assignment: {name!r}")
        if len(values) != block.width:
            raise ValueError(
                f"MILP bounds block {name!r} has width {len(values)}, "
                f"expected {block.width}"
            )
        for index, value in enumerate(values, start=block.offset):
            lower, upper = value
            if upper is not None and lower > upper:
                raise ValueError(f"invalid bounds in block {name!r}: {value!r}")
            self._bounds[index] = (
                float(lower),
                None if upper is None else float(upper),
            )
        self._assigned.add(name)

    def fill(self, name: str, value: Bound) -> None:
        """Write one named block filled with a single repeated bound."""
        self.set(name, [value] * self._layout.width(name))

    def finalize(self) -> list[Bound]:
        """Return complete bounds, failing when any block was never written."""
        missing = sorted(
            block.name
            for block in self._layout.blocks
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
        return self._layout.blocks


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
