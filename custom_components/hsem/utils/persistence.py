"""Pure validation helpers for JSON-restored integration state."""

from __future__ import annotations

import json
import math
import os
import stat
import tempfile
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Hard cap on history-file size. Generous for bounded, pruned JSON logs;
#: exists to stop a swapped-in oversized file from exhausting memory.
_MAX_HISTORY_FILE_BYTES = 16 * 1024 * 1024

# os.O_NOFOLLOW is POSIX-only; HSEM only runs under Home Assistant (Linux),
# but fall back to a no-op flag rather than raising on other platforms.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def finite_float(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Return a bounded finite float, or ``None`` for invalid state."""
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except TypeError, ValueError, OverflowError:
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def aware_datetime_from_iso(value: Any) -> datetime | None:
    """Parse an aware ISO datetime, returning ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except TypeError, ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        parsed.astimezone(UTC)
    except OverflowError, ValueError:
        return None
    return parsed


def read_json_history_file(path: Path) -> dict[str, Any] | None:
    """Read and parse a bounded JSON history file from disk.

    Rejects symlinks and non-regular files via ``lstat`` (so a swapped-in
    symlink is never followed to stat its target) and re-checks with
    ``O_NOFOLLOW`` at open time to close the TOCTOU window between the stat
    and the read. Files over the size cap are rejected before parsing.

    Args:
        path: Path to the JSON history file.

    Returns:
        The parsed JSON object as a dict, or ``None`` if the file is
        missing, not a plain file, too large, a symlink, or invalid JSON.
    """
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_HISTORY_FILE_BYTES:
            return None
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError, json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def write_json_history_file(path: Path, data: dict[str, Any]) -> bool:
    """Write a JSON history file to disk atomically.

    The data goes to a temporary file in the same directory, which then
    replaces the target in one step, so a crash or power loss mid-write
    leaves the previous file intact. Blocking: call it from a thread.

    Args:
        path: Path of the JSON history file. Its directory is created when
            missing.
        data: The JSON-serialisable state to persist.

    Returns:
        ``True`` if the file was written, ``False`` on an I/O error.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix=f".{path.stem}_", dir=str(path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_path, path)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp_path)
            raise
    except OSError:
        return False
    return True
