"""Tests for the unfilled-state gate (issue #1398).

``coordinator._ocpp_sessions`` was created as ``[]``, read every cycle and
never filled, so ``sensor.hsem_ocpp_charger_sessions_sensor`` showed 0 with full
coverage (issue #1392). ``scripts/check_unfilled_state.py`` fails on that
shape. Its rules, its allowlist, its failure paths and the state of the real
tree are pinned here.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

from tests.script_loader import REPO_ROOT, load_script

_CHECKER = load_script("check_unfilled_state")


def _package(root: Path, **files: str) -> Path:
    """Write *files* (name -> source) into a fake integration below *root*."""
    package = root / _CHECKER.PACKAGE
    package.mkdir(parents=True)
    for name, source in files.items():
        (package / f"{name}.py").write_text(textwrap.dedent(source), "utf-8")
    return root


def _names(root: Path) -> list[str]:
    """Return the unfilled attribute names below *root*, ignoring the allowlist."""
    offenders, _, _ = _CHECKER.find_unfilled(root, allowlist={})
    return [item.name for item in offenders]


_SESSIONS_BUG = """
    class Coordinator:
        def __init__(self):
            self._sessions = []

        def cycle(self):
            return list(self._sessions)
"""


class TestRules:
    """What counts as unfilled."""

    def test_the_sessions_bug_is_reported(self, tmp_path: Path) -> None:
        """Created empty, read through list(), never filled: reported."""
        assert _names(_package(tmp_path, a=_SESSIONS_BUG)) == ["_sessions"]

    @pytest.mark.parametrize(
        "empty", ["None", "0", "False", '""', "[]", "{}", "()", "set()", "deque()"]
    )
    def test_every_empty_value_counts(self, tmp_path: Path, empty: str) -> None:
        """Each empty literal or empty constructor counts as unfilled."""
        source = f"""
            class C:
                def __init__(self):
                    self._x = {empty}

                def read(self):
                    return self._x
        """
        assert _names(_package(tmp_path, a=source)) == ["_x"]

    @pytest.mark.parametrize(
        "fill",
        [
            "self._x.append(1)",
            "self._x.update({1: 2})",
            "self._x[1] = 2",
            "self._x[1][2] = 3",
            "self._x += [1]",
            "self._x = [1]",
            "self._x, self._y = [1], 2",
            "fill(self._x)",
            "fill(target=self._x)",
            "setattr(self, '_x', [1])",
            "Data(_x=[1])",
        ],
    )
    def test_any_fill_passes(self, tmp_path: Path, fill: str) -> None:
        """A mutation, a real value, a callee or a keyword counts as a fill."""
        source = f"""
            class C:
                def __init__(self):
                    self._x = []

                def work(self):
                    {fill}
                    return self._x
        """
        assert "_x" not in _names(_package(tmp_path, a=source))

    def test_a_fill_in_another_module_passes(self, tmp_path: Path) -> None:
        """Names are matched across the package, not per file."""
        root = _package(
            tmp_path,
            a=_SESSIONS_BUG,
            b="def record(c):\n    c._sessions.append(1)\n",
        )
        assert _names(root) == []

    def test_an_attribute_never_read_is_not_reported(self, tmp_path: Path) -> None:
        """Dead state is vulture's job; only state somebody reads matters here."""
        source = "class C:\n    def __init__(self):\n        self._x = []\n"
        assert _names(_package(tmp_path, a=source)) == []

    def test_an_empty_keyword_is_not_a_fill(self, tmp_path: Path) -> None:
        """Passing an empty value by keyword does not fill the attribute."""
        source = textwrap.dedent(_SESSIONS_BUG) + "\nData(_sessions=[])\n"
        assert _names(_package(tmp_path, a=source)) == ["_sessions"]

    def test_the_location_and_read_count_are_reported(self, tmp_path: Path) -> None:
        """The report names every empty assignment and the number of reads."""
        offenders, _, checked = _CHECKER.find_unfilled(
            _package(tmp_path, a=_SESSIONS_BUG), allowlist={}
        )
        (item,) = offenders
        assert item.locations == (f"{_CHECKER.PACKAGE}/a.py:4",)
        assert item.reads == 1
        assert checked == 1


class TestAllowlist:
    """Allowlisted names pass, stale entries fail."""

    def test_an_allowlisted_name_passes(self, tmp_path: Path) -> None:
        """A listed name is not reported."""
        offenders, stale, _ = _CHECKER.find_unfilled(
            _package(tmp_path, a=_SESSIONS_BUG), allowlist={"_sessions": "why"}
        )
        assert offenders == []
        assert stale == []

    def test_an_entry_that_is_no_longer_needed_is_reported(
        self, tmp_path: Path
    ) -> None:
        """A listed name that is filled now, or gone, is stale."""
        _, stale, _ = _CHECKER.find_unfilled(
            _package(tmp_path, a="x = 1\n"), allowlist={"_gone": "why"}
        )
        assert stale == ["_gone"]

    def test_every_entry_has_a_reason(self) -> None:
        """An entry is a debt with a reason, never a bare exemption."""
        assert all(reason.strip() for reason in _CHECKER.ALLOWLIST.values())


class TestMain:
    """Exit codes and messages."""

    def test_it_fails_and_names_the_attribute(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unfilled attribute fails with its name and location."""
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {})
        root = _package(tmp_path, a=_SESSIONS_BUG)
        assert _CHECKER.main(["--root", str(root)]) == 1
        err = capsys.readouterr().err
        assert "_sessions (1 reads)" in err
        assert "a.py:4" in err

    def test_it_fails_on_a_stale_entry(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stale allowlist entry fails the run."""
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {"_gone": "why"})
        root = _package(tmp_path, a="x = 1\n")
        assert _CHECKER.main(["--root", str(root)]) == 1
        assert "_gone" in capsys.readouterr().err

    def test_it_passes_a_clean_tree(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A clean tree passes and says how many files it parsed."""
        monkeypatch.setattr(_CHECKER, "ALLOWLIST", {})
        root = _package(tmp_path, a="x = 1\n")
        assert _CHECKER.main(["--root", str(root)]) == 0
        assert "[ok] no unfilled state in 1 files" in capsys.readouterr().out

    def test_it_fails_when_the_package_is_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A wrong working directory cannot pass."""
        assert _CHECKER.main(["--root", str(tmp_path)]) == 1
        assert "[error] directory not found" in capsys.readouterr().err


class TestRepository:
    """The real tree passes, and the gate is wired in everywhere."""

    def test_the_repository_has_no_unfilled_state(self) -> None:
        """Nothing unfilled and no allowlist entry left over."""
        offenders, stale, checked = _CHECKER.find_unfilled(REPO_ROOT)

        assert offenders == []
        assert stale == []
        assert checked > 0

    def test_the_quality_script_and_the_pre_commit_hook_run_the_check(self) -> None:
        """The check is reachable from every place that is meant to run it."""
        script = (REPO_ROOT / "scripts" / "quality.sh").read_text(encoding="utf-8")
        hooks = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

        assert "run python3 scripts/check_unfilled_state.py" in script
        # Once each for the unfilled-state, format-check (CI) and all targets.
        assert len(re.findall(r"^ +unfilled_state_check$", script, re.MULTILINE)) == 3
        assert "args: [unfilled-state]" in hooks
