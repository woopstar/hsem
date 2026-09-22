"""Fixtures for the planner backtest harness (issue #1037)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from custom_components.hsem.models.planner_input import PlannerInput
from tests.backtest.replay import ReplayReport, iter_dumps, planner_input_from_dict

CORPUS_DIR = Path(__file__).parent / "corpus"

#: Point this at a directory of extra dumps to replay a private corpus that is
#: too large (or too personal) to commit.  A real collection run is roughly
#: 2.6 MB/day, so the committed corpus stays a sample and the bulk lives
#: outside the repo.
CORPUS_ENV_VAR = "HSEM_BACKTEST_CORPUS"

#: Cap on cycles read from any one corpus file.  A three-week append log holds
#: thousands of cycles at ~0.15 s each; replaying all of them would blow the
#: per-test timeout with no warning.  Raise it deliberately when that is what
#: you want.
MAX_CYCLES_ENV_VAR = "HSEM_BACKTEST_MAX_CYCLES"
DEFAULT_MAX_CYCLES = 25


def corpus_paths() -> list[Path]:
    """Return every corpus file the harness should replay.

    Returns:
        The committed corpus, plus anything in the directory named by
        ``HSEM_BACKTEST_CORPUS``, sorted so parametrised test IDs are stable.
        Both ``*.json`` (one cycle) and ``*.jsonl`` (an append log) are found.

    Raises:
        ValueError: If ``HSEM_BACKTEST_CORPUS`` names a missing directory.  A
            typo that silently replayed nothing would look like a clean run.
    """

    def _find(directory: Path) -> list[Path]:
        return sorted(
            [*directory.glob("*.json"), *directory.glob("*.jsonl")],
            key=lambda p: p.name,
        )

    paths = _find(CORPUS_DIR)
    external = os.environ.get(CORPUS_ENV_VAR)
    if external:
        extra = Path(external)
        if not extra.is_dir():
            raise ValueError(f"{CORPUS_ENV_VAR}={external!r} is not a directory")
        paths.extend(_find(extra))
    return paths


def max_cycles() -> int:
    """Return how many cycles to read from one corpus file."""
    raw = os.environ.get(MAX_CYCLES_ENV_VAR)
    return int(raw) if raw else DEFAULT_MAX_CYCLES


def replayed_cycles(path: Path) -> Iterator[tuple[int, PlannerInput, ReplayReport]]:
    """Yield each rebuilt cycle in a corpus file, capped by :func:`max_cycles`.

    Args:
        path: The corpus file.

    Yields:
        ``(index, planner_input, report)`` for each cycle, in file order.
    """
    for index, payload in enumerate(iter_dumps(path)):
        if index >= max_cycles():
            return
        planner_input, report = planner_input_from_dict(payload)
        yield index, planner_input, report


@pytest.fixture(params=corpus_paths(), ids=lambda p: p.stem)
def corpus_dump(request: pytest.FixtureRequest) -> Path:
    """Yield each corpus file in turn."""
    path: Path = request.param
    return path
