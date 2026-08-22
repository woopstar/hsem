"""Named MILP bounds assembly with strict structural validation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Bound = tuple[float, float | None]


@dataclass(frozen=True)
class MilpBoundsBlock:
    """One declared contiguous bounds block."""

    name: str
    offset: int
    width: int


class MilpBoundsBuilder:
    """Assign bounds by named declared offsets and reject layout corruption."""

    def __init__(self, column_count: int) -> None:
        if column_count < 0:
            raise ValueError("column_count must be non-negative")
        self._bounds: list[Bound | None] = [None] * column_count
        self._blocks: dict[str, MilpBoundsBlock] = {}

    def set(self, name: str, offset: int, values: Sequence[Bound]) -> None:
        """Assign one named block at its exact column offset."""
        if not name or name in self._blocks:
            raise ValueError(f"duplicate or empty bounds block: {name!r}")
        width = len(values)
        if offset < 0 or offset + width > len(self._bounds):
            raise ValueError(
                f"bounds block {name!r} [{offset}:{offset + width}] exceeds "
                f"column count {len(self._bounds)}"
            )
        if any(
            self._bounds[index] is not None for index in range(offset, offset + width)
        ):
            raise ValueError(f"bounds block {name!r} overlaps an assigned column")
        for index, value in enumerate(values, start=offset):
            lower, upper = value
            if upper is not None and lower > upper:
                raise ValueError(f"invalid bounds in block {name!r}: {value!r}")
            self._bounds[index] = (
                float(lower),
                None if upper is None else float(upper),
            )
        self._blocks[name] = MilpBoundsBlock(name, offset, width)

    def fill(self, name: str, offset: int, width: int, value: Bound) -> None:
        """Assign *width* repeated bounds at *offset*."""
        if width < 0:
            raise ValueError("bounds block width must be non-negative")
        self.set(name, offset, [value] * width)

    def finalize(self) -> list[Bound]:
        """Return complete bounds, failing when any column is unassigned."""
        missing = [index for index, value in enumerate(self._bounds) if value is None]
        if missing:
            preview = ", ".join(str(index) for index in missing[:10])
            raise ValueError(f"unassigned MILP bounds columns: {preview}")
        return [value for value in self._bounds if value is not None]

    @property
    def blocks(self) -> tuple[MilpBoundsBlock, ...]:
        """Return blocks ordered by physical column offset."""
        return tuple(sorted(self._blocks.values(), key=lambda block: block.offset))
