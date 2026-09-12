"""Tests for utils.persistence.read_json_history_file.

Covers the SAST-flagged path-traversal / symlink-following read findings
(SKY-D215, SKY-D325) across the tracker history files: valid data, missing
files, corrupt JSON, oversized files, and symlinked files must all be
rejected or handled without raising.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from custom_components.hsem.utils.persistence import (
    _MAX_HISTORY_FILE_BYTES,
    read_json_history_file,
)


def test_reads_valid_json_dict(tmp_path: Path) -> None:
    """A well-formed JSON object is parsed and returned."""
    path = tmp_path / "history.json"
    path.write_text(json.dumps({"days": [1, 2, 3]}), encoding="utf-8")
    assert read_json_history_file(path) == {"days": [1, 2, 3]}


def test_missing_file_returns_none(tmp_path: Path) -> None:
    """A nonexistent path returns None instead of raising."""
    assert read_json_history_file(tmp_path / "missing.json") is None


def test_corrupt_json_returns_none(tmp_path: Path) -> None:
    """Invalid JSON content is treated as absent history, not an error."""
    path = tmp_path / "corrupt.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert read_json_history_file(path) is None


def test_non_dict_json_returns_none(tmp_path: Path) -> None:
    """A JSON array (or other non-object top level) is rejected."""
    path = tmp_path / "array.json"
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert read_json_history_file(path) is None


def test_directory_returns_none(tmp_path: Path) -> None:
    """A directory at the given path is rejected, not opened."""
    directory = tmp_path / "a_directory"
    directory.mkdir()
    assert read_json_history_file(directory) is None


def test_oversized_file_returns_none(tmp_path: Path) -> None:
    """Files over the size cap are rejected before parsing."""
    path = tmp_path / "huge.json"
    with path.open("w", encoding="utf-8") as handle:
        handle.write('{"pad": "')
        handle.seek(_MAX_HISTORY_FILE_BYTES + 1024)
        handle.write('"}')
    assert path.stat().st_size > _MAX_HISTORY_FILE_BYTES
    assert read_json_history_file(path) is None


def test_symlink_to_valid_file_is_rejected(tmp_path: Path) -> None:
    """A symlink is never followed, even if the target is valid JSON."""
    real_target = tmp_path / "real.json"
    real_target.write_text(json.dumps({"days": []}), encoding="utf-8")

    link = tmp_path / "link.json"
    os.symlink(real_target, link)

    assert read_json_history_file(link) is None
