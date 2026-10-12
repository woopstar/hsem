"""Tests for the file-size gate (issue #1277).

The 30 KiB / 1000-line rule was only a paragraph and two ``find`` one-liners,
so ``utils/dynamic_floor.py`` grew to 35 KB without anything failing (issue
#1274). ``scripts/check_file_size.py`` is the gate now, so its limits, its
allowlist and its failure paths are pinned here, and so is the state of the
real tree.

See ``CODE_QUALITY_STANDARDS.md`` → File Size Limits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.script_loader import REPO_ROOT, load_script

_CHECKER = load_script("check_file_size")
_SCOPE = _CHECKER.Scope("pkg", max_bytes=100, max_lines=10)


def _write(root: Path, name: str, *, size: int, lines: int) -> Path:
    """Write a file of exactly *size* bytes and *lines* lines below *root*."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n" * (lines - 1)
    path.write_text(body + "x" * (size - len(body) - 1) + "\n", encoding="utf-8")
    return path


def _repository(root: Path) -> Path:
    """Create the directories of every scope below *root*."""
    for scope in _CHECKER.SCOPES:
        (root / scope.directory).mkdir(parents=True)
    return root


class TestLimits:
    """The limits are the documented ones, and they are inclusive."""

    def test_the_integration_limit_is_30_kib_and_1000_lines(self) -> None:
        """The shipped integration gets 30 KiB / 1000 lines, dev code twice that."""
        assert _CHECKER.MAX_FILE_BYTES == 30720
        assert _CHECKER.MAX_FILE_LINES == 1000
        assert (
            _CHECKER.Scope("custom_components/hsem", 30720, 1000),
            _CHECKER.Scope("scripts", 61440, 2000),
            _CHECKER.Scope("tests", 61440, 2000),
        ) == _CHECKER.SCOPES

    def test_a_file_over_the_size_limit_is_reported(self, tmp_path: Path) -> None:
        """One byte too many fails even when the line count is fine."""
        _write(tmp_path, "pkg/big.py", size=101, lines=10)

        offenders, stale, checked = _CHECKER.find_offenders(tmp_path, (_SCOPE,), {})

        assert [(o.path, o.size, o.lines) for o in offenders] == [
            ("pkg/big.py", 101, 10)
        ]
        assert stale == []
        assert checked == 1

    def test_a_file_over_the_line_limit_is_reported(self, tmp_path: Path) -> None:
        """One line too many fails even when the size is fine."""
        _write(tmp_path, "pkg/sub/long.py", size=100, lines=11)

        offenders, _stale, _checked = _CHECKER.find_offenders(tmp_path, (_SCOPE,), {})

        assert [(o.path, o.size, o.lines) for o in offenders] == [
            ("pkg/sub/long.py", 100, 11)
        ]

    def test_a_file_at_both_limits_passes(self, tmp_path: Path) -> None:
        """The limit is the last passing value, not the first failing one."""
        _write(tmp_path, "pkg/exact.py", size=100, lines=10)

        assert _CHECKER.find_offenders(tmp_path, (_SCOPE,), {}) == ([], [], 1)

    def test_a_last_line_without_a_newline_is_counted(self, tmp_path: Path) -> None:
        """An unterminated last line is a line, unlike in ``wc -l``."""
        path = tmp_path / "pkg/unterminated.py"
        path.parent.mkdir()
        path.write_text("x\n" * 10 + "x", encoding="utf-8")

        offenders, _stale, _checked = _CHECKER.find_offenders(tmp_path, (_SCOPE,), {})

        assert [o.lines for o in offenders] == [11]

    def test_only_python_files_are_checked(self, tmp_path: Path) -> None:
        """JSON, YAML and Markdown are out of scope."""
        _write(tmp_path, "pkg/data.json", size=500, lines=50)
        _write(tmp_path, "pkg/small.py", size=10, lines=1)

        assert _CHECKER.find_offenders(tmp_path, (_SCOPE,), {}) == ([], [], 1)

    def test_each_scope_applies_its_own_limits(self, tmp_path: Path) -> None:
        """The same file passes in a scope with more room."""
        _write(tmp_path, "pkg/big.py", size=150, lines=5)
        _write(tmp_path, "dev/big.py", size=150, lines=5)
        scopes = (_SCOPE, _CHECKER.Scope("dev", max_bytes=200, max_lines=20))

        offenders, _stale, checked = _CHECKER.find_offenders(tmp_path, scopes, {})

        assert [o.path for o in offenders] == ["pkg/big.py"]
        assert checked == 2


class TestAllowlist:
    """An allowlist entry exempts a file only for as long as it is needed."""

    def test_an_allowlisted_file_passes(self, tmp_path: Path) -> None:
        """A file over the limit with an entry is not an offender."""
        _write(tmp_path, "pkg/big.py", size=500, lines=50)

        result = _CHECKER.find_offenders(
            tmp_path, (_SCOPE,), {"pkg/big.py": "split in #1"}
        )

        assert result == ([], [], 1)

    def test_an_entry_that_is_no_longer_needed_is_reported(
        self, tmp_path: Path
    ) -> None:
        """A file back within the limits, or gone, must leave the list."""
        _write(tmp_path, "pkg/small.py", size=10, lines=1)
        allowlist = {"pkg/small.py": "split in #1", "pkg/gone.py": "split in #2"}

        offenders, stale, _checked = _CHECKER.find_offenders(
            tmp_path, (_SCOPE,), allowlist
        )

        assert offenders == []
        assert stale == ["pkg/gone.py", "pkg/small.py"]

    def test_every_entry_names_the_issue_that_splits_the_file(self) -> None:
        """An exemption without an issue is a permission, not a debt."""
        for path, reason in _CHECKER.ALLOWLIST.items():
            assert "#" in reason, path


class TestMain:
    """The command names every offender and fails closed."""

    def test_it_fails_and_names_the_file_its_size_and_its_lines(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Each offender is printed with both measurements and both limits."""
        root = _repository(tmp_path)
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {})
        _write(root, "custom_components/hsem/big.py", size=30721, lines=3)
        _write(root, "custom_components/hsem/long.py", size=2000, lines=1001)
        _write(root, "tests/test_big.py", size=30721, lines=1001)

        assert _CHECKER.main(["--root", str(root)]) == 1

        err = capsys.readouterr().err
        assert "2 of 3 files exceed the file-size limit" in err
        assert (
            "custom_components/hsem/big.py: 30721 bytes (limit 30720), "
            "3 lines (limit 1000)"
        ) in err
        assert (
            "custom_components/hsem/long.py: 2000 bytes (limit 30720), "
            "1001 lines (limit 1000)"
        ) in err
        # A test module of that size is within the doubled limit.
        assert "tests/test_big.py" not in err

    def test_it_passes_at_the_limit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Files exactly at their scope's limits are accepted."""
        root = _repository(tmp_path)
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {})
        _write(root, "custom_components/hsem/exact.py", size=30720, lines=1000)
        _write(root, "scripts/exact.py", size=61440, lines=2000)

        assert _CHECKER.main(["--root", str(root)]) == 0
        assert (
            "[ok] all 2 files are within the file-size limits"
            in capsys.readouterr().out
        )

    def test_it_reports_how_many_files_are_allowlisted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A pass with exemptions does not claim every file is within the limits."""
        root = _repository(tmp_path)
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {"scripts/big.py": "split in #1"})
        _write(root, "scripts/big.py", size=61441, lines=3)
        _write(root, "scripts/small.py", size=10, lines=1)

        assert _CHECKER.main(["--root", str(root)]) == 0
        assert (
            "[ok] 1 of 2 files are within the file-size limits, 1 are allowlisted"
            in capsys.readouterr().out
        )

    def test_it_fails_on_a_stale_allowlist_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A leftover entry fails the gate, so the list cannot go stale."""
        root = _repository(tmp_path)
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {"scripts/gone.py": "split in #1"})

        assert _CHECKER.main(["--root", str(root)]) == 1

        err = capsys.readouterr().err
        assert "1 allowlisted files are within the limits" in err
        assert "  scripts/gone.py" in err

    def test_it_fails_when_a_directory_is_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A wrong root must not pass by finding nothing to check."""
        assert _CHECKER.main(["--root", str(tmp_path)]) == 1
        assert "[error] directory not found" in capsys.readouterr().err


class TestRepository:
    """The real tree passes, and the gate is wired into the quality script."""

    def test_every_file_of_the_repository_is_within_its_limits(self) -> None:
        """No file is over a limit and no allowlist entry is left over."""
        offenders, stale, checked = _CHECKER.find_offenders(REPO_ROOT)

        assert offenders == []
        assert stale == []
        assert checked > 0

    def test_the_quality_script_and_the_pre_commit_hook_run_the_check(self) -> None:
        """The check is reachable from every place that is meant to run it."""
        script = (REPO_ROOT / "scripts" / "quality.sh").read_text(encoding="utf-8")
        hooks = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

        assert "run python3 scripts/check_file_size.py" in script
        # Once each for the file-size, format-check (CI) and all targets.
        assert len(re.findall(r"^ +file_size_check$", script, re.MULTILINE)) == 3
        assert "args: [file-size]" in hooks
