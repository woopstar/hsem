"""Tests for the schema-library compatibility module (issue #1289).

Home Assistant 2026.10 replaced voluptuous with probatio and aliases
``voluptuous`` to it at start-up.  HSEM has to load on Home Assistant versions
before and after that, so it imports ``voluptuous`` at runtime and lets type
checkers see ``probatio``.  These tests pin both halves, and that no module
goes around the one place that decides.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from custom_components.hsem.utils import schema as schema_module
from custom_components.hsem.utils.schema import vol

_PACKAGE = Path(schema_module.__file__).resolve().parents[1]
_SCHEMA_LIBRARIES = {"voluptuous", "probatio"}


def _imported_modules(tree: ast.AST) -> set[str]:
    """Return the top-level names of every module *tree* imports."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


class TestRuntime:
    def test_vol_is_voluptuous_at_runtime(self) -> None:
        """Whatever ``voluptuous`` is here: the library, or probatio's alias."""
        assert vol is sys.modules["voluptuous"]

    def test_probatio_is_only_imported_for_type_checking(self) -> None:
        """Home Assistant before 2026.10 does not ship probatio."""
        tree = ast.parse(Path(schema_module.__file__).read_text(encoding="utf-8"))
        (branch,) = [node for node in tree.body if isinstance(node, ast.If)]

        assert ast.unparse(branch.test) == "TYPE_CHECKING"
        assert _imported_modules(ast.Module(body=branch.body)) == {"probatio"}
        assert _imported_modules(ast.Module(body=branch.orelse)) == {"voluptuous"}
        runtime = [node for node in tree.body if not isinstance(node, ast.If)]
        assert not _imported_modules(ast.Module(body=runtime)) & _SCHEMA_LIBRARIES

    def test_a_schema_validates_and_reports_errors(self) -> None:
        """The API HSEM's flows use behaves the same under either library."""
        schema = vol.Schema(
            {
                vol.Required("name"): str,
                vol.Optional("amps", default=16): vol.All(
                    vol.Coerce(int), vol.Range(min=6, max=32)
                ),
            }
        )

        assert schema({"name": "garage"}) == {"name": "garage", "amps": 16}
        with pytest.raises(vol.Invalid):
            schema({"name": "garage", "amps": 99})

    def test_home_assistant_accepts_the_schema_as_its_own(self) -> None:
        """From 2026.10 on a schema built through ``vol`` is a probatio schema."""
        probatio = pytest.importorskip("probatio")
        import homeassistant  # noqa: F401, PLC0415 — installs the voluptuous alias

        schema = vol.Schema({vol.Required("name"): str})

        assert isinstance(schema, probatio.Schema)
        assert vol.UNDEFINED is probatio.UNDEFINED
        assert probatio.to_field_list(schema) == [
            {"type": "string", "name": "name", "required": True}
        ]


class TestOnePlaceDecides:
    def test_no_other_module_imports_a_schema_library(self) -> None:
        """A direct ``import voluptuous`` type-checks as the wrong library."""
        offenders = {
            str(path.relative_to(_PACKAGE)): sorted(
                _imported_modules(ast.parse(path.read_text(encoding="utf-8")))
                & _SCHEMA_LIBRARIES
            )
            for path in sorted(_PACKAGE.rglob("*.py"))
            if path != Path(schema_module.__file__).resolve()
        }

        assert {name: found for name, found in offenders.items() if found} == {}

    def test_the_check_would_catch_an_offender(self) -> None:
        tree = ast.parse("import voluptuous as vol\nfrom probatio import Schema\n")

        assert _imported_modules(tree) == _SCHEMA_LIBRARIES

    def test_the_modules_that_build_schemas_use_the_shared_name(self) -> None:
        users = [
            path
            for path in _PACKAGE.rglob("*.py")
            if "from custom_components.hsem.utils.schema import vol"
            in path.read_text(encoding="utf-8")
        ]

        assert len(users) >= 25
