#!/usr/bin/env python3
"""Check that the LaTeX math in ``docs/`` survives prettier and GitHub.

Two tools rewrite math that is written carelessly, and neither reports it:

- **prettier** treats a display formula written on one line
  (``$$ C_{total} = ... $$``) as ordinary text.  It reads the underscores as
  emphasis and turns ``C_{total}`` into ``C*{total}`` or ``p\\_{imp}``
  (issue #1184).  A block with ``$$`` alone on its own lines is left alone.
- **GitHub** applies Markdown's backslash escapes inside ``$...$`` and
  ``$$...$$``: a backslash before ASCII punctuation is dropped, so ``\\_``
  becomes a subscript, ``\\,`` a comma and ``\\{`` an opening group.  A
  ``\\\\`` in the middle of a line becomes a single backslash.  Lines inside a
  block that look like Markdown (a list marker, a quote, a blank line) end
  the block.

The rules this script enforces follow from that:

1. Display math uses the block form: ``$$`` alone on a line, the formula,
   ``$$`` alone on a line.
2. No formula contains the marks prettier leaves behind: ``*{``, ``\\_{`` or
   an asterisk between two word characters.
3. Inside math a backslash before punctuation is doubled (``\\\\_``,
   ``\\\\,``, ``\\\\{``), and a ``\\\\`` line break sits at the end of its line.
4. No line inside a block is blank or starts like a Markdown list, quote,
   heading or table.

A fenced ```` ```math ```` block and the inline form ``$`...`$`` are passed to
the renderer untouched by both tools, so they are not checked.

Usage::

    python3 scripts/check_docs_math.py            # checks docs/**/*.md
    python3 scripts/check_docs_math.py FILE ...   # checks the given files

Exit status is 0 when every formula is clean and 1 otherwise.
"""

from __future__ import annotations

import re
import string
import sys
from dataclasses import dataclass
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS_DIR = _REPO_ROOT / "docs"

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.DOTALL)
#: GitHub's inline rule: the opening ``$`` has a non-space to its right, the
#: closing ``$`` a non-space to its left and no digit after it.
_INLINE_MATH = re.compile(
    r"(?<![\\$])\$(?![\s$`])((?:[^$\\]|\\.)+?)(?<![\s\\])\$(?![$\d])", re.DOTALL
)
_MARKDOWN_LINE = re.compile(r"^ {0,3}([-+*] |\d+[.)] |#{1,6} |>|\|)")
_PRETTIER_MARKS = (
    (re.compile(r"\*\{"), "'*{': a subscript rewritten by prettier, write '_{'"),
    (
        re.compile(r"(?<!\\)\\_\{"),
        "'\\_{': a subscript escaped by prettier, write '_{'",
    ),
    (
        re.compile(r"(?<=[A-Za-z0-9}])\*(?=[A-Za-z0-9])"),
        "'*' between word characters: a subscript rewritten by prettier "
        "(use \\cdot or \\times for a product)",
    ),
)
_PUNCTUATION = frozenset(string.punctuation) - {"\\"}


@dataclass(frozen=True)
class Violation:
    """One problem found in a document."""

    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.message}"


def _escape_problems(tex: str) -> list[tuple[int, str]]:
    """Return ``(line offset, message)`` for backslashes GitHub would change.

    Args:
        tex: The LaTeX source of one formula.

    Returns:
        One entry per single backslash before punctuation, and per ``\\\\``
        that is followed by more text on the same line.
    """
    problems: list[tuple[int, str]] = []
    for offset, line in enumerate(tex.split("\n")):
        i = 0
        while i < len(line):
            if line[i] != "\\":
                i += 1
                continue
            following = line[i + 1 : i + 2]
            if following == "\\":
                rest = line[i + 2 :]
                if rest.strip() and rest[0] not in _PUNCTUATION:
                    problems.append(
                        (
                            offset,
                            "'\\\\' in the middle of a line: GitHub turns it into "
                            "a single backslash, end the line after it",
                        )
                    )
                i += 2
                continue
            # '\\_{' is reported once, as the mark prettier leaves behind.
            if following in _PUNCTUATION and line[i + 1 : i + 3] != "_{":
                problems.append(
                    (
                        offset,
                        f"'\\{following}': GitHub drops the backslash inside math, "
                        f"write '\\\\{following}'",
                    )
                )
            i += 2
    return problems


def _check_formula(path: Path, line: int, tex: str) -> list[Violation]:
    """Return the violations of rules 2 and 3 in one formula."""
    violations = [
        Violation(path, line + tex.count("\n", 0, match.start()), message)
        for pattern, message in _PRETTIER_MARKS
        for match in pattern.finditer(tex)
    ]
    violations += [
        Violation(path, line + offset, message)
        for offset, message in _escape_problems(tex)
    ]
    return violations


def _blank_out(match: re.Match[str]) -> str:
    """Replace a match with spaces, keeping its line breaks."""
    return re.sub(r"[^\n]", " ", match.group(0))


def check_text(path: Path, text: str) -> list[Violation]:
    """Return every math violation in one Markdown document.

    Args:
        path: The document's path, used in the report only.
        text: The document's content.

    Returns:
        The violations in document order.
    """
    violations: list[Violation] = []
    lines = text.split("\n")
    prose: list[str] = []
    fence: str | None = None
    block_start: int | None = None
    block_indent = 0
    block: list[str] = []

    for number, line in enumerate(lines, 1):
        fence_match = _FENCE.match(line)
        if block_start is None and fence_match:
            marker = fence_match.group(1)[0]
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            prose.append("")
            continue
        if fence is not None:
            prose.append("")
            continue

        if line.strip() == "$$":
            if block_start is None:
                block_start = number
                block_indent = len(line) - len(line.lstrip())
                block = []
            else:
                violations += _check_formula(path, block_start + 1, "\n".join(block))
                block_start = None
            prose.append("")
            continue

        if block_start is not None:
            content = line[block_indent:] if line[:block_indent].isspace() else line
            if not content.strip():
                violations.append(
                    Violation(path, number, "blank line inside a $$ block ends it")
                )
            elif _MARKDOWN_LINE.match(content):
                violations.append(
                    Violation(
                        path,
                        number,
                        "line inside a $$ block starts like Markdown "
                        f"({content.split()[0]!r}) and ends the block",
                    )
                )
            block.append(line)
            prose.append("")
            continue

        prose.append(line)

    if block_start is not None:
        violations.append(Violation(path, block_start, "$$ block is never closed"))

    # Inline math and single-line display math live in the remaining prose.
    remaining = _INLINE_CODE.sub(_blank_out, "\n".join(prose))
    for number, line in enumerate(remaining.split("\n"), 1):
        if line.count("$$") >= 2:
            violations.append(
                Violation(
                    path,
                    number,
                    "display math on one line: prettier rewrites it, put each "
                    "$$ on its own line",
                )
            )
    paragraph_start = 0
    for paragraph in re.split(r"\n[ \t]*\n", remaining):
        first_line = remaining.count("\n", 0, paragraph_start) + 1
        if "$$" not in paragraph:
            for match in _INLINE_MATH.finditer(paragraph):
                line = first_line + paragraph.count("\n", 0, match.start(1))
                violations += _check_formula(path, line, match.group(1))
        paragraph_start = remaining.find(paragraph, paragraph_start) + len(paragraph)

    return sorted(violations, key=lambda v: (v.line, v.message))


def check_files(paths: list[Path]) -> list[Violation]:
    """Return the violations of every file in *paths*."""
    violations: list[Violation] = []
    for path in paths:
        violations += check_text(path, path.read_text(encoding="utf-8"))
    return violations


def main(argv: list[str]) -> int:
    """Check the given files, or every Markdown file under ``docs/``.

    Args:
        argv: File paths; empty to check ``docs/**/*.md``.

    Returns:
        ``0`` when clean, ``1`` when a formula breaks a rule or a file is
        missing.
    """
    paths = [Path(arg) for arg in argv] or sorted(_DOCS_DIR.rglob("*.md"))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        for path in missing:
            print(f"[fail] {path}: no such file", file=sys.stderr)
        return 1
    if not paths:
        print(f"[fail] no Markdown files found under {_DOCS_DIR}", file=sys.stderr)
        return 1

    violations = check_files(paths)
    for violation in violations:
        print(violation, file=sys.stderr)
    if violations:
        print(
            f"[fail] {len(violations)} math problem(s) in "
            f"{len({v.path for v in violations})} file(s); see the rules at "
            "the top of scripts/check_docs_math.py",
            file=sys.stderr,
        )
        return 1
    print(f"[ok] math in {len(paths)} Markdown file(s) is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
