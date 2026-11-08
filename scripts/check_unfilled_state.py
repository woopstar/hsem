#!/usr/bin/env python3
"""Fail when an attribute is created empty, read, and never given a value.

``sensor.hsem_ocpp_charger_sessions_sensor`` showed 0 from the day it was added:
``coordinator._ocpp_sessions`` was created as ``[]``, read every cycle, and
never filled (issue #1392). Coverage could not see it, because the line that
reads the list runs, and the sensor's test built its data by hand (issue
#1398).

This check parses every ``*.py`` file below ``custom_components/hsem`` and
reports an attribute when all of these hold:

- every assignment to it is an empty literal (``None``, ``0``, ``False``,
  ``""``, ``[]``, ``{}``, ``()``, or a call such as ``set()`` or ``deque()``
  without arguments)
- it is never mutated (``.append()``, ``.update()``, ``x[k] = ...``,
  augmented assignment ...)
- it is never passed to a callee that may fill it (passing it to a builtin
  such as ``list()`` or ``len()`` does not count)
- it is never set through a keyword argument or ``setattr()``
- it is read somewhere

Names are matched across the whole package, not per class, so the check
never reports an attribute that something, somewhere, does fill. It can
miss a case, but it does not cry wolf.

Usage:
    python3 scripts/check_unfilled_state.py [--root PATH]
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "custom_components/hsem"

# Attributes exempt from the check: name -> reason. An entry is a debt, not a
# permission: name the issue that removes it. A stale entry fails the check.
ALLOWLIST: dict[str, str] = {
    "_intercept": (
        "ConsumptionPredictor keeps a fixed 0 intercept, the one-hot weekday "
        "columns carry it; dead but harmless"
    ),
}

# Methods that change a container in place.
MUTATORS = frozenset(
    {
        "add",
        "append",
        "appendleft",
        "clear",
        "discard",
        "extend",
        "extendleft",
        "insert",
        "pop",
        "popitem",
        "remove",
        "reverse",
        "setdefault",
        "sort",
        "update",
    }
)

# Callables that read their argument and never fill it.
READ_ONLY_CALLS = frozenset(
    {
        "all",
        "any",
        "bool",
        "copy",
        "deepcopy",
        "dict",
        "enumerate",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "iter",
        "len",
        "list",
        "max",
        "min",
        "repr",
        "reversed",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
    }
)

_EMPTY_CONSTRUCTORS = frozenset(
    {"defaultdict", "deque", "dict", "frozenset", "list", "OrderedDict", "set"}
)


@dataclass(frozen=True)
class Unfilled:
    """An attribute that is created empty, read, and never filled."""

    name: str
    locations: tuple[str, ...]
    reads: int


def is_empty_literal(node: ast.expr) -> bool:
    """Return whether *node* is an empty value.

    Args:
        node: The assigned expression.

    Returns:
        ``True`` for ``None``, ``0``, ``False``, ``""``, an empty list,
        dict, set or tuple display, or an argument-less call of a container
        constructor.
    """
    if isinstance(node, ast.Constant):
        return node.value is None or node.value in (0, "", False)
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return not node.elts
    if isinstance(node, ast.Dict):
        return not node.keys
    if isinstance(node, ast.Call) and not node.args and not node.keywords:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else None
        return name in _EMPTY_CONSTRUCTORS
    return False


def _root_attribute(node: ast.expr) -> ast.Attribute | None:
    """Return the attribute under a chain of subscripts, if any.

    Args:
        node: ``x.attr``, ``x.attr[k]`` or ``x.attr[k][j]``.

    Returns:
        The ``x.attr`` node, or ``None`` when the chain does not end in one.
    """
    while isinstance(node, ast.Subscript):
        node = node.value
    return node if isinstance(node, ast.Attribute) else None


class _Collector(ast.NodeVisitor):
    """Count empty assignments, fills and reads per attribute name."""

    def __init__(self) -> None:
        self.empty: defaultdict[str, list[str]] = defaultdict(list)
        self.fills: Counter[str] = Counter()
        self.reads: Counter[str] = Counter()
        self.location = ""

    def _store(self, target: ast.expr, value: ast.expr | None) -> None:
        if isinstance(target, ast.Attribute):
            if value is not None and is_empty_literal(value):
                self.empty[target.attr].append(f"{self.location}:{target.lineno}")
            else:
                self.fills[target.attr] += 1
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._store(element, None)
            return
        attribute = _root_attribute(target)
        if attribute is not None:
            self.fills[attribute.attr] += 1

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._store(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._store(node.target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._store(node.target, None)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in MUTATORS:
            attribute = _root_attribute(func.value)
            if attribute is not None:
                self.fills[attribute.attr] += 1
        if not (isinstance(func, ast.Name) and func.id in READ_ONLY_CALLS):
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                attribute = _root_attribute(arg)
                if attribute is not None:
                    self.fills[attribute.attr] += 1
        if (
            isinstance(func, ast.Name)
            and func.id == "setattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            self.fills[node.args[1].value] += 1
        for keyword in node.keywords:
            if keyword.arg and not is_empty_literal(keyword.value):
                self.fills[keyword.arg] += 1
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load):
            self.reads[node.attr] += 1
        self.generic_visit(node)


def find_unfilled(
    root: Path, allowlist: dict[str, str] | None = None
) -> tuple[list[Unfilled], list[str], int]:
    """Find every unfilled attribute and every allowlist entry no longer needed.

    Args:
        root: The repository root.
        allowlist: Attribute names exempt from the check, with a reason;
            defaults to ``ALLOWLIST``.

    Returns:
        The unfilled attributes that are not allowlisted, the allowlisted
        names that are no longer unfilled, and the number of files parsed.

    Raises:
        FileNotFoundError: If the package directory does not exist.
    """
    allowlist = ALLOWLIST if allowlist is None else allowlist
    package = root / PACKAGE
    if not package.is_dir():
        raise FileNotFoundError(package)
    collector = _Collector()
    files = sorted(package.rglob("*.py"))
    for path in files:
        collector.location = path.relative_to(root).as_posix()
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), str(path)))

    unfilled = {
        name: Unfilled(name, tuple(locations), collector.reads[name])
        for name, locations in sorted(collector.empty.items())
        if collector.fills[name] == 0 and collector.reads[name] > 0
    }
    offenders = [item for name, item in unfilled.items() if name not in allowlist]
    stale = sorted(name for name in allowlist if name not in unfilled)
    return offenders, stale, len(files)


def main(argv: list[str] | None = None) -> int:
    """Check the package for unfilled attributes.

    Args:
        argv: Command-line arguments, defaulting to ``sys.argv[1:]``.

    Returns:
        ``0`` when nothing is unfilled, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)

    try:
        offenders, stale, checked = find_unfilled(args.root)
    except FileNotFoundError as exc:
        print(f"[error] directory not found: {exc}", file=sys.stderr)
        return 1

    if offenders:
        print(
            f"[error] {len(offenders)} attributes are created empty, read, and "
            "never filled:",
            file=sys.stderr,
        )
        for item in offenders:
            print(
                f"  {item.name} ({item.reads} reads), set empty at "
                f"{', '.join(item.locations)}",
                file=sys.stderr,
            )
        print(
            "\n[info] Fill it where the data is produced, or remove it. A test "
            "that builds the data by hand does not prove the producer works.",
            file=sys.stderr,
        )

    if stale:
        print(
            f"[error] {len(stale)} allowlisted attributes are filled now (or "
            "gone); remove them from ALLOWLIST in scripts/check_unfilled_state.py:",
            file=sys.stderr,
        )
        for name in stale:
            print(f"  {name}", file=sys.stderr)

    if offenders or stale:
        return 1

    print(f"[ok] no unfilled state in {checked} files, {len(ALLOWLIST)} allowlisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
