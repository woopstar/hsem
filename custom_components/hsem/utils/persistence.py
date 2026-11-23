"""Validation helpers and guarded file I/O for JSON-restored integration state.

A tracker restores its own history file through :func:`async_load_history`
(issue #1446). A missing file starts the tracker empty. A file that cannot be
read is left alone and is not written until a later load succeeds. A file
whose content cannot be used is moved to ``<name>.corrupt-<timestamp>``
before a new one is written.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from custom_components.hsem.utils.logger import HSEM_LOGGER as _LOGGER

#: Hard cap on history-file size. Generous for bounded, pruned JSON logs;
#: exists to stop a swapped-in oversized file from exhausting memory.
_MAX_HISTORY_FILE_BYTES = 16 * 1024 * 1024

# os.O_NOFOLLOW is POSIX-only; HSEM only runs under Home Assistant (Linux),
# but fall back to a no-op flag rather than raising on other platforms.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

# Outcome of one read of a history file.
_READ_OK = "ok"
_READ_MISSING = "missing"
_READ_UNREADABLE = "unreadable"
_READ_INVALID = "invalid"

#: History files whose last load failed while the file is still on disk.
#: ``write_json_history_file`` refuses these paths, so a tracker that started
#: empty after a failed read cannot replace the history it could not load.
_PENDING_LOADS: set[Path] = set()


class HistoryBacked(Protocol):
    """A tracker that restores itself from its own history file."""

    history_file: str

    async def load_history(self) -> None:
        """Restore the tracker from its history file."""


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


def _read_history_file(
    path: Path, max_bytes: int
) -> tuple[str, dict[str, Any] | None, str]:
    """Read a history file and classify the outcome.

    Rejects symlinks and non-regular files via ``lstat`` (so a swapped-in
    symlink is never followed to stat its target) and re-checks with
    ``O_NOFOLLOW`` at open time to close the TOCTOU window between the stat
    and the read. Files over the size cap are rejected before parsing.

    Returns:
        The outcome, the parsed object when the outcome is ``_READ_OK``, and
        a short reason for any other outcome.
    """
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return _READ_INVALID, None, "it is not a regular file"
        if info.st_size > max_bytes:
            return _READ_INVALID, None, f"it is larger than {max_bytes} bytes"
        fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW)
    except FileNotFoundError:
        return _READ_MISSING, None, "it does not exist"
    except OSError as err:
        return _READ_UNREADABLE, None, str(err)
    try:
        with os.fdopen(fd, encoding="utf-8") as handle:
            payload = json.load(handle)
    except OSError as err:
        return _READ_UNREADABLE, None, str(err)
    except ValueError, RecursionError:
        return _READ_INVALID, None, "it is not valid JSON"
    if not isinstance(payload, dict):
        return _READ_INVALID, None, "it does not hold a JSON object"
    return _READ_OK, payload, ""


def read_json_history_file(
    path: Path, *, max_bytes: int = _MAX_HISTORY_FILE_BYTES
) -> dict[str, Any] | None:
    """Read and parse a bounded JSON history file from disk.

    Never changes anything on disk. A tracker restoring its own file uses
    :func:`async_load_history`, which also protects a file that failed to
    load from being overwritten.

    Args:
        path: Path to the JSON history file.
        max_bytes: Largest file size that is read.

    Returns:
        The parsed JSON object as a dict, or ``None`` if the file is
        missing, not a plain file, too large, a symlink, or invalid JSON.
    """
    return _read_history_file(path, max_bytes)[1]


def history_load_pending(path: Path) -> bool:
    """Return whether the last load of a history file failed.

    Args:
        path: Path of the JSON history file.

    Returns:
        ``True`` while the file is on disk but has not been loaded. It is
        not written in that state.
    """
    return path in _PENDING_LOADS


def set_aside_history_file(path: Path, reason: str) -> bool:
    """Move an unusable history file out of the way, keeping its content.

    The file is renamed to ``<name>.corrupt-<UTC timestamp>`` in the same
    directory, so a new history file can be written without losing the old
    one. When the rename fails the file stays where it is and is not
    written. Blocking: call it from a thread.

    Args:
        path: Path of the JSON history file.
        reason: Why the file cannot be used, for the log.

    Returns:
        ``True`` if the file was moved.
    """
    # Microseconds, so two files set aside in the same second do not collide.
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    kept = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        os.replace(path, kept)
    except OSError as err:
        _mark_load_pending(path, f"{reason}, and it could not be moved aside ({err})")
        return False
    _PENDING_LOADS.discard(path)
    _LOGGER.warning(
        "History file %s cannot be used: %s. It is kept as %s and a new "
        "history is started",
        path,
        reason,
        kept.name,
    )
    return True


def _mark_load_pending(path: Path, reason: str) -> None:
    """Block writes to a history file that could not be loaded; warn once."""
    if path in _PENDING_LOADS:
        return
    _PENDING_LOADS.add(path)
    _LOGGER.warning(
        "History file %s could not be read: %s. It is left unchanged and "
        "nothing is saved to it until it loads",
        path,
        reason,
    )


def load_json_history_file(
    path: Path, *, max_bytes: int = _MAX_HISTORY_FILE_BYTES
) -> dict[str, Any] | None:
    """Read a tracker's own history file for a restore.

    - A missing file returns ``None`` without a warning.
    - A file that cannot be read (``OSError``) stays on disk and is marked
      as pending: :func:`write_json_history_file` refuses it until a later
      call of this function gets through.
    - A file that is not valid JSON, not a JSON object, not a regular file
      or too large is moved aside by :func:`set_aside_history_file`.

    Blocking: call it from a thread.

    Args:
        path: Path to the JSON history file.
        max_bytes: Largest file size that is read.

    Returns:
        The parsed JSON object, or ``None`` when there is nothing to restore.
    """
    outcome, payload, reason = _read_history_file(path, max_bytes)
    if outcome == _READ_UNREADABLE:
        _mark_load_pending(path, reason)
    elif outcome == _READ_INVALID:
        set_aside_history_file(path, reason)
    else:
        _PENDING_LOADS.discard(path)
    return payload


async def async_load_history(
    path: Path, restore: Callable[[dict[str, Any]], object]
) -> None:
    """Load a tracker's history file and hand its content to ``restore``.

    The shared load of every tracker (issue #1446). See
    :func:`load_json_history_file` for a missing or unreadable file. Content
    that ``restore`` rejects by raising is moved aside the same way as
    invalid JSON, so the next save cannot replace it.

    Args:
        path: Path to the JSON history file.
        restore: Applies the parsed JSON object to the tracker.
    """
    was_pending = path in _PENDING_LOADS
    data = await asyncio.to_thread(load_json_history_file, path)
    if data is None:
        return
    try:
        restore(data)
    except Exception as err:
        if was_pending:
            # The warning for this file is out already: if the move fails
            # again, the retry stays silent.
            _PENDING_LOADS.add(path)
        await asyncio.to_thread(
            set_aside_history_file, path, f"its content could not be restored ({err!r})"
        )


async def ensure_history_loaded(tracker: HistoryBacked) -> bool:
    """Retry the load of a tracker whose history file could not be read.

    Called before a save. A tracker whose file loaded, or was missing, is
    left alone.

    Args:
        tracker: The tracker that is about to be saved.

    Returns:
        ``False`` while the file still cannot be read, in which case the
        tracker must not be saved.
    """
    history_file = getattr(tracker, "history_file", None)
    if not isinstance(history_file, str) or not history_file:
        return True
    path = Path(history_file)
    if not history_load_pending(path):
        return True
    await tracker.load_history()
    return not history_load_pending(path)


def write_json_history_file(
    path: Path, data: Mapping[str, Any], *, indent: int | None = None
) -> bool:
    """Write a JSON history file to disk atomically.

    The data goes to a temporary file in the same directory, which then
    replaces the target in one step, so a crash or power loss mid-write
    leaves the previous file intact. Blocking: call it from a thread.

    Args:
        path: Path of the JSON history file. Its directory is created when
            missing.
        data: The JSON-serialisable state to persist.
        indent: Indentation of the written JSON; compact when ``None``.

    Returns:
        ``True`` if the file was written, ``False`` on an I/O error or while
        the existing file is waiting for a load that failed (see
        :func:`load_json_history_file`).
    """
    if path in _PENDING_LOADS:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix=f".{path.stem}_", dir=str(path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    data,
                    handle,
                    ensure_ascii=False,
                    indent=indent,
                    separators=None if indent is not None else (",", ":"),
                )
            os.replace(tmp_path, path)
        except Exception:
            with suppress(OSError):
                os.unlink(tmp_path)
            raise
    except OSError:
        return False
    return True
