"""Tests for the docs math checker (issue #1184).

Prettier rewrote 18 formulas in the docs (``C_{total}`` became ``C*{total}``)
because they were written as ``$$ ... $$`` on one line, and nothing reported
it for months.  GitHub's own Markdown escapes break more: ``\\_`` inside math
loses its backslash.  ``scripts/check_docs_math.py`` is the gate against both,
so its rules are pinned here, and so is the state of the real ``docs/`` tree.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "scripts" / "check_docs_math.py"


def _load() -> ModuleType:
    """Load the checker script as a module."""
    spec = importlib.util.spec_from_file_location("check_docs_math", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_CHECKER = _load()
_DOC = Path("docs/example.md")


def _messages(text: str) -> list[tuple[int, str]]:
    """Return ``(line, message)`` for every violation in *text*."""
    return [(v.line, v.message) for v in _CHECKER.check_text(_DOC, text)]


def _lines(text: str) -> list[int]:
    return [line for line, _message in _messages(text)]


class TestCleanMath:
    """Forms that both prettier and GitHub leave alone."""

    def test_multi_line_block_is_clean(self) -> None:
        text = "Cost:\n\n$$\nC_{total} = C_{import} - R_{export}\n$$\n"

        assert _messages(text) == []

    def test_doubled_escapes_are_clean(self) -> None:
        text = (
            "$$\n"
            "net\\\\_load[t] = \\mathrm{max\\\\_grid} \\\\, x \\in \\\\{1, 2\\\\}\n"
            "$$\n"
        )

        assert _messages(text) == []

    def test_line_break_at_the_end_of_a_line_is_clean(self) -> None:
        text = "$$\n\\begin{aligned}\na &= b \\\\\nc &= d\n\\end{aligned}\n$$\n"

        assert _messages(text) == []

    def test_inline_math_with_subscripts_is_clean(self) -> None:
        text = "Costs $c_{cycle} \\cdot \\max(x, x) = c_{cycle} \\cdot x$, so.\n"

        assert _messages(text) == []

    def test_indented_block_inside_a_list_is_clean(self) -> None:
        text = "- item:\n\n  $$\n  a_{b} + c\n  $$\n"

        assert _messages(text) == []

    def test_indented_operator_row_is_not_a_list(self) -> None:
        """Four spaces of indentation cannot start a list inside a paragraph."""
        text = "$$\n\\begin{aligned}\n    + & x \\\\\n    - & y\n\\end{aligned}\n$$\n"

        assert _messages(text) == []

    def test_fenced_code_and_inline_code_are_ignored(self) -> None:
        text = (
            "Write `$$ a_{b} $$` like this:\n\n"
            "```text\n$$ C*{import} = \\sum*{t} p\\_{imp} $$\n```\n\n"
            "```math\nnet\\_load = a \\\\ b\n```\n"
        )

        assert _messages(text) == []

    def test_backtick_inline_math_is_ignored(self) -> None:
        """GitHub passes ``$`...`$`` through untouched."""
        assert _messages("Use $`purchase\\_price`$ here.\n") == []

    def test_prices_in_prose_are_not_math(self) -> None:
        assert _messages("It costs $5 now and $6 later, or 3 * 2.\n") == []


class TestSingleLineDisplayMath:
    """Rule 1: prettier rewrites a display formula written on one line."""

    @pytest.mark.parametrize(
        "line",
        [
            "$$ C_{total} = C_{import} $$",
            "$$C_{total} = C_{import}$$",
            "  $$ a_{b} $$",
            "Text before $$ a_{b} $$ and after.",
        ],
    )
    def test_is_reported(self, line: str) -> None:
        messages = _messages(f"Intro\n\n{line}\n")

        assert [number for number, _ in messages] == [3]
        assert "display math on one line" in messages[0][1]

    def test_unclosed_block_is_reported(self) -> None:
        messages = _messages("$$\na = b\n\nmore text\n")

        assert (1, "$$ block is never closed") in messages


class TestPrettierMarks:
    """Rule 2: what prettier leaves behind in a mangled formula."""

    @pytest.mark.parametrize(
        ("tex", "fragment"),
        [
            ("C*{total} = C*{import}", "'*{'"),
            ("p\\_{imp}[t]", "'\\_{'"),
            ("w*1 \\cdot avg_1", "'*' between word characters"),
            ("0.70 \\cdot avg*7", "'*' between word characters"),
        ],
    )
    def test_mangled_block_is_reported(self, tex: str, fragment: str) -> None:
        messages = _messages(f"$$\n{tex}\n$$\n")

        assert messages
        assert all(number == 2 for number, _ in messages)
        assert any(fragment in message for _, message in messages)

    def test_mangled_inline_math_is_reported(self) -> None:
        """The form the issue found at ``cost-function-math.md:59``."""
        text = "costs $c*{cycle} \\cdot \\max(x, x) = c\\_{cycle} \\cdot x$,\n"

        messages = _messages(text)

        assert [number for number, _ in messages] == [1, 1]

    def test_the_formulas_from_the_issue_are_reported(self) -> None:
        mangled = (
            "$$\nC*{import} = \\sum*{t \\in slots} gi[t] \\cdot p\\_{imp}[t]\n$$\n"
        )

        assert len(_messages(mangled)) == 3

    def test_spaced_product_is_not_a_mark(self) -> None:
        assert _messages("$$\na * b\n$$\n") == []


class TestGithubEscapes:
    """Rule 3: GitHub drops a backslash before punctuation inside math."""

    @pytest.mark.parametrize("char", ["_", ",", ";", "%", "{", "}", "|", "!", "#"])
    def test_single_backslash_before_punctuation_is_reported(self, char: str) -> None:
        messages = _messages(f"$$\na \\{char} b\n$$\n")

        assert [number for number, _ in messages] == [2]
        assert f"write '\\\\{char}'" in messages[0][1]

    def test_single_backslash_in_inline_math_is_reported(self) -> None:
        messages = _messages("where $\\mathrm{usable\\_kwh}$ is the range.\n")

        assert [number for number, _ in messages] == [1]

    def test_latex_commands_are_not_escapes(self) -> None:
        text = "$$\n\\frac{a}{b} \\cdot \\left( c \\right) \\ d \\quad e\n$$\n"

        assert _messages(text) == []

    def test_line_break_in_the_middle_of_a_line_is_reported(self) -> None:
        messages = _messages("$$\na = b \\\\ c = d\n$$\n")

        assert [number for number, _ in messages] == [2]
        assert "middle of a line" in messages[0][1]

    def test_inline_math_wrapped_over_two_lines_is_checked(self) -> None:
        text = "The set $W = \\{t \\le T\n\\}$ is the window.\n"

        assert _lines(text) == [1, 2]


class TestMarkdownInsideBlock:
    """Rule 4: a line that looks like Markdown ends the block on GitHub."""

    @pytest.mark.parametrize(
        "line", ["+ \\beta \\cdot x", "- y", "* z", "> \\frac{a}{b}", "1. x", "# x"]
    )
    def test_markdown_looking_line_is_reported(self, line: str) -> None:
        messages = _messages(f"$$\na = b\n{line}\n$$\n")

        assert [number for number, _ in messages] == [3]
        assert "starts like Markdown" in messages[0][1]

    def test_blank_line_is_reported(self) -> None:
        messages = _messages("$$\na = b\n\n+ c\n$$\n")

        assert (3, "blank line inside a $$ block ends it") in messages


class TestCommandLine:
    """The script's exit status is what gates CI."""

    def test_clean_file_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = tmp_path / "clean.md"
        doc.write_text("$$\nC_{total} = 1\n$$\n", encoding="utf-8")

        assert _CHECKER.main([str(doc)]) == 0
        assert "[ok] math in 1 Markdown file(s) is clean" in capsys.readouterr().out

    def test_mangled_file_exits_one_and_names_the_line(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        doc = tmp_path / "mangled.md"
        doc.write_text("Intro\n\n$$ C*{total} = 1 $$\n", encoding="utf-8")

        assert _CHECKER.main([str(doc)]) == 1
        err = capsys.readouterr().err
        assert f"{doc}:3: display math on one line" in err
        assert "[fail] 1 math problem(s) in 1 file(s)" in err

    def test_missing_file_exits_one(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert _CHECKER.main([str(tmp_path / "absent.md")]) == 1
        assert "no such file" in capsys.readouterr().err

    def test_empty_docs_directory_fails_loudly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A gate that checked nothing must not pass by default."""
        monkeypatch.setattr(_CHECKER, "_DOCS_DIR", tmp_path)

        assert _CHECKER.main([]) == 1
        assert "no Markdown files found" in capsys.readouterr().err


class TestRepositoryDocs:
    """The real docs tree stays clean."""

    def test_docs_tree_has_no_math_violations(self) -> None:
        """Includes the acceptance criterion of issue #1184: no formula in
        ``docs/`` carries ``*{`` or ``\\_{``.
        """
        docs = sorted((_REPO_ROOT / "docs").rglob("*.md"))

        assert len(docs) > 20
        assert [str(v) for v in _CHECKER.check_files(docs)] == []
