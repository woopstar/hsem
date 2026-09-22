"""Fixtures for the Stage 1 planner backtest harness (issue #1037)."""

from __future__ import annotations

from pathlib import Path

import pytest

CORPUS_DIR = Path(__file__).parent / "corpus"


def corpus_paths() -> list[Path]:
    """Return every committed diagnostics dump in the corpus.

    Returns:
        The corpus JSON files, sorted by name so parametrised test IDs are
        stable across machines.
    """
    return sorted(CORPUS_DIR.glob("*.json"))


@pytest.fixture(params=corpus_paths(), ids=lambda p: p.stem)
def corpus_dump(request: pytest.FixtureRequest) -> Path:
    """Yield each committed corpus dump in turn."""
    path: Path = request.param
    return path
