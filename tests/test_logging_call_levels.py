"""Regression guard: never pass a log-level string to a logger method.

``async_log(level, msg, *args)`` / ``log_planner(level, msg, *args)`` take the
level as their first argument. ``HSEM_LOGGER`` is a plain
:class:`logging.Logger`, so ``_LOGGER.debug("msg", "warning")`` treats
``"warning"`` as a ``%``-format argument: the record is always DEBUG (dropped
when verbose logging is off) and formatting raises ``TypeError: not all
arguments converted`` when verbose logging is on (issue #1114).
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent.parent / "custom_components" / "hsem"
_LOG_METHODS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})
_LEVEL_STRINGS = frozenset({"debug", "info", "warning", "error", "critical"})


def _receiver_name(node: ast.expr) -> str | None:
    """Return the trailing name of a call receiver (``_LOGGER``, ``self._logger``)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _stray_level_calls(source: str, filename: str) -> list[str]:
    """Return ``file:line`` for every logger call with a level-string argument."""
    offenders: list[str] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _LOG_METHODS
        ):
            continue
        receiver = _receiver_name(node.func.value)
        if receiver is None or not receiver.lower().endswith("logger"):
            continue
        if any(
            isinstance(arg, ast.Constant)
            and isinstance(arg.value, str)
            and arg.value.lower() in _LEVEL_STRINGS
            for arg in node.args
        ):
            offenders.append(f"{filename}:{node.lineno}")
    return offenders


class TestStrayLevelArgumentGuard:
    """No ``*LOGGER.<level>()`` call passes a level string positionally."""

    def test_detector_flags_the_bug_pattern(self) -> None:
        """The scanner catches the exact pattern fixed in issue #1114."""
        source = (
            "_LOGGER.debug('Entity not configured; skipping write', 'warning')\n"
            "HSEM_LOGGER.debug(f'write FAILED for {x}', 'error')\n"
            "self._logger.info('msg', 'critical')\n"
        )
        assert _stray_level_calls(source, "sample.py") == [
            "sample.py:1",
            "sample.py:2",
            "sample.py:3",
        ]

    def test_detector_ignores_correct_usage(self) -> None:
        """Level-first helpers and ordinary ``%s`` arguments are fine."""
        source = (
            "async_log('warning', 'Entity %s unavailable', entity_id)\n"
            "log_planner('info', 'msg')\n"
            "_LOGGER.warning('Mode is %s', mode)\n"
            "_LOGGER.debug('Level %s', level_name)\n"
        )
        assert _stray_level_calls(source, "sample.py") == []

    def test_package_has_no_stray_level_arguments(self) -> None:
        """Every module under ``custom_components/hsem`` is clean."""
        files = sorted(_PACKAGE.rglob("*.py"))
        assert files, f"no Python files found under {_PACKAGE}"
        offenders = [
            offender
            for path in files
            for offender in _stray_level_calls(
                path.read_text(encoding="utf-8"),
                str(path.relative_to(_PACKAGE.parent.parent)),
            )
        ]
        assert offenders == [], (
            "Logger calls pass a log-level string as a %-format argument; call "
            "the matching logger method instead: " + ", ".join(offenders)
        )
