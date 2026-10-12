#!/usr/bin/env python3
"""Enforce the file-size rule: no Python file grows past its size and line limit.

The rule (``CODE_QUALITY_STANDARDS.md`` → File Size Limits) keeps every module
small enough to read and review in one go. Nothing else enforces it: ruff has
no file-level limit and ``check-added-large-files`` only looks at files of
hundreds of kilobytes, which is how ``utils/dynamic_floor.py`` reached 35 KB
over several PRs without a check failing (split in issue #1274).

Walks every directory in ``SCOPES``, measures each ``*.py`` file, and exits
non-zero if a file exceeds its size or its line limit. A file exactly at a
limit passes.

Usage:
    python3 scripts/check_file_size.py [--root PATH]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

KIB = 1024

# The limits of the shipped integration; see CODE_QUALITY_STANDARDS.md.
MAX_FILE_BYTES = 30 * KIB
MAX_FILE_LINES = 1000

DEFAULT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Scope:
    """A directory and the limits of every ``*.py`` file below it."""

    directory: str
    max_bytes: int
    max_lines: int


@dataclass(frozen=True)
class Offender:
    """A file over a limit of its scope."""

    path: str
    size: int
    lines: int
    scope: Scope


# Dev-only scripts get twice the room of the integration. ``tests/`` is not a
# scope (issue #1278): a test module is a list of independent cases that grows
# with every regression test, and a limit there would make a fix carry an
# unrelated split of its test file.
SCOPES: tuple[Scope, ...] = (
    Scope("custom_components/hsem", MAX_FILE_BYTES, MAX_FILE_LINES),
    Scope("scripts", 2 * MAX_FILE_BYTES, 2 * MAX_FILE_LINES),
)

# Files exempt from their scope's limits: repo-relative path -> reason.
# An entry is a debt, not a permission: name the issue that will split the
# file, and remove the entry once it is back within the limits.
ALLOWLIST: dict[str, str] = {}


def _measure(path: Path) -> tuple[int, int]:
    """Return the size in bytes and the number of lines of *path*.

    Args:
        path: The file to measure.

    Returns:
        The file's size in bytes and its line count.
    """
    data = path.read_bytes()
    return len(data), len(data.splitlines())


def find_offenders(
    root: Path,
    scopes: tuple[Scope, ...] = SCOPES,
    allowlist: dict[str, str] | None = None,
) -> tuple[list[Offender], list[str], int]:
    """Find every file over a limit and every allowlist entry no longer needed.

    Args:
        root: The repository root the scope directories are relative to.
        scopes: The directories to walk and their limits.
        allowlist: Repo-relative paths exempt from the limits, with a reason;
            defaults to ``ALLOWLIST``.

    Returns:
        The files over a limit that are not allowlisted, the allowlisted paths
        that are within the limits (or gone), and the number of files checked.

    Raises:
        FileNotFoundError: If a scope directory does not exist below *root*.
    """
    allowlist = ALLOWLIST if allowlist is None else allowlist
    offenders: list[Offender] = []
    over_limit: set[str] = set()
    checked = 0
    for scope in scopes:
        directory = root / scope.directory
        if not directory.is_dir():
            raise FileNotFoundError(directory)
        for path in sorted(directory.rglob("*.py")):
            size, lines = _measure(path)
            checked += 1
            if size <= scope.max_bytes and lines <= scope.max_lines:
                continue
            name = path.relative_to(root).as_posix()
            over_limit.add(name)
            if name not in allowlist:
                offenders.append(Offender(name, size, lines, scope))
    stale = sorted(name for name in allowlist if name not in over_limit)
    return offenders, stale, checked


def main(argv: list[str] | None = None) -> int:
    """Check every file in scope against the file-size limits.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when every file is within its limits, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)

    try:
        offenders, stale, checked = find_offenders(args.root)
    except FileNotFoundError as exc:
        print(f"[error] directory not found: {exc}", file=sys.stderr)
        return 1

    if offenders:
        print(
            f"[error] {len(offenders)} of {checked} files exceed the file-size "
            "limit (CODE_QUALITY_STANDARDS.md → File Size Limits):",
            file=sys.stderr,
        )
        for offender in offenders:
            print(
                f"  {offender.path}: {offender.size} bytes "
                f"(limit {offender.scope.max_bytes}), {offender.lines} lines "
                f"(limit {offender.scope.max_lines})",
                file=sys.stderr,
            )
        print(
            "\n[info] Split the file by responsibility before adding to it. "
            "Do not raise the limit.",
            file=sys.stderr,
        )

    if stale:
        print(
            f"[error] {len(stale)} allowlisted files are within the limits "
            "(or gone); remove them from ALLOWLIST in scripts/check_file_size.py:",
            file=sys.stderr,
        )
        for name in stale:
            print(f"  {name}", file=sys.stderr)

    if offenders or stale:
        return 1

    if ALLOWLIST:
        print(
            f"[ok] {checked - len(ALLOWLIST)} of {checked} files are within the "
            f"file-size limits, {len(ALLOWLIST)} are allowlisted"
        )
    else:
        print(f"[ok] all {checked} files are within the file-size limits")
    return 0


if __name__ == "__main__":
    sys.exit(main())
