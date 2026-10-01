"""The planner guide shows only explanation values the planner emits (issue #1240).

``docs/planner-guide.md`` used to show ``constraints`` tags and
``selected_strategy`` values (``export_price_above_threshold``, ``solar_only``,
``baseline``) that ``_build_explanation`` never produces, so a user comparing
the guide with ``sensor.hsem_plan_explanation_sensor`` looked for values that
could not appear.  These tests read the guide and the sensors reference and
check every tag and strategy they show against the string literals in
``custom_components/hsem/planner/engine_explanation.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SOURCE = (
    _REPO_ROOT / "custom_components" / "hsem" / "planner" / "engine_explanation.py"
)
_DOCS = [
    _REPO_ROOT / "docs" / "planner-guide.md",
    _REPO_ROOT / "docs" / "sensors-reference.md",
]

# ``"constraints": ["a", "b"]`` in JSON excerpts and ``constraints: [a, b]`` in
# the YAML-style attribute listing.
_CONSTRAINTS_LINE = re.compile(r"""["']?constraints["']?\s*:\s*\[([^\]]*)\]""")
_STRATEGY_LINE = re.compile(
    r"""["']?selected_strategy["']?\s*:\s*["']?([a-z_]+)["']?"""
)
_TAG_IN_LIST = re.compile(r"[a-z_]+")
_TABLE_TAG = re.compile(r"^\|\s*`([a-z_]+)`\s*\|", re.MULTILINE)


def _emitted(kind: str) -> set[str]:
    """Return the values ``_build_explanation`` can emit for *kind*."""
    source = _SOURCE.read_text(encoding="utf-8")
    if kind == "constraints":
        return set(re.findall(r'constraints\.append\("([a-z_]+)"\)', source))
    return set(re.findall(r'selected_strategy = "([a-z_]+)"', source))


def _shown_constraints(text: str) -> set[str]:
    """Return every tag shown in a ``constraints`` list in *text*."""
    found: set[str] = set()
    for match in _CONSTRAINTS_LINE.finditer(text):
        found.update(_TAG_IN_LIST.findall(match.group(1)))
    return found


def _shown_strategies(text: str) -> set[str]:
    """Return every ``selected_strategy`` value shown in *text*."""
    return set(_STRATEGY_LINE.findall(text))


def _constraints_table(text: str) -> set[str]:
    """Return the tags in the guide's *Understanding constraints* table."""
    start = text.index("### Understanding `constraints`")
    end = text.index("\n## ", start)
    return set(_TABLE_TAG.findall(text[start:end]))


class TestEmittedValues:
    """Sanity checks on the source scan, so an empty set cannot pass."""

    def test_source_lists_constraint_tags(self) -> None:
        assert {"winter_month", "summer_month", "no_price_spread"} <= _emitted(
            "constraints"
        )

    def test_source_lists_strategies(self) -> None:
        assert {"charge_grid_discharge_peak", "winter_wait"} <= _emitted("strategy")


class TestGuideShowsOnlyEmittedValues:
    """Every value the docs show must be one the code can emit."""

    @pytest.mark.parametrize("doc", _DOCS, ids=lambda p: p.name)
    def test_constraint_tags(self, doc: Path) -> None:
        shown = _shown_constraints(doc.read_text(encoding="utf-8"))

        assert shown <= _emitted("constraints")

    @pytest.mark.parametrize("doc", _DOCS, ids=lambda p: p.name)
    def test_selected_strategies(self, doc: Path) -> None:
        shown = _shown_strategies(doc.read_text(encoding="utf-8"))

        assert shown <= _emitted("strategy")

    def test_guide_shows_excerpts(self) -> None:
        guide = _DOCS[0].read_text(encoding="utf-8")

        assert len(_shown_constraints(guide)) >= 3
        assert len(_shown_strategies(guide)) >= 2

    def test_constraints_table_lists_exactly_the_emitted_tags(self) -> None:
        guide = _DOCS[0].read_text(encoding="utf-8")

        assert _constraints_table(guide) == _emitted("constraints")


class TestScanners:
    """The scanners catch the forms the guide uses, so a regression is seen."""

    def test_json_and_yaml_constraints_are_read(self) -> None:
        text = (
            '  "constraints": ["summer_month", "no_such_tag"],\n'
            "    constraints: [winter_month, other_tag]\n"
        )

        assert _shown_constraints(text) == {
            "summer_month",
            "no_such_tag",
            "winter_month",
            "other_tag",
        }

    def test_json_and_yaml_strategies_are_read(self) -> None:
        text = (
            '  "selected_strategy": "solar_only",\n'
            "    selected_strategy: charge_grid_discharge_peak\n"
        )

        assert _shown_strategies(text) == {"solar_only", "charge_grid_discharge_peak"}

    def test_retired_values_fail(self) -> None:
        shown = _shown_strategies('"selected_strategy": "baseline"')

        assert not shown <= _emitted("strategy")
