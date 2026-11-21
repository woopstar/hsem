"""Guard: every file HSEM writes under ``.storage`` is in the backup (issue #1447).

``hsem.export_data`` backs up the files named in ``HISTORY_FILES``. Four
stores were added without being named there, so they were left out of the
backup, the import and the crash recovery. These tests read the integration
source and fail when a module writes a ``.storage`` file the list does not
name.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from custom_components.hsem.utils.backup import HISTORY_FILES, PENDING_IMPORT_FILE

INTEGRATION = Path(__file__).parents[1] / "custom_components" / "hsem"

#: The module that defines the backup list.
BACKUP_MODULE = "utils/backup.py"

#: Modules that use ``.storage`` only to read and write the files of the
#: backup list, and so name no file of their own.
STORAGE_MODULES_WITHOUT_OWN_FILE = {"services_backup.py"}

#: Modules allowed to use Home Assistant's ``Store``. The dashboard marker
#: only records that the dashboard was created once; it is not learned data.
STORE_MODULES = {"utils/dashboard.py"}

_JSON_FILE_NAME = re.compile(r"^[\w.-]+\.json$")


def _string_constants(path: Path) -> set[str]:
    """Return every string literal in the module at *path*."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _json_names(constants: set[str]) -> set[str]:
    """Return the string literals that are a JSON file name."""
    return {value for value in constants if _JSON_FILE_NAME.match(value)}


def _modules() -> dict[str, set[str]]:
    """Return the string literals of every integration module but the backup."""
    return {
        relative: _string_constants(path)
        for path in sorted(INTEGRATION.rglob("*.py"))
        if (relative := path.relative_to(INTEGRATION).as_posix()) != BACKUP_MODULE
    }


def unbacked_storage_files(
    modules: dict[str, set[str]], backed_up: set[str]
) -> dict[str, set[str]]:
    """Return, per module, the ``.storage`` files a backup would miss.

    A module that uses ``.storage`` must name its files as plain literals,
    all of them in *backed_up*. A module that names none (the name is built
    at run time) is reported with an empty set.
    """
    missing: dict[str, set[str]] = {}
    for relative, constants in modules.items():
        if ".storage" not in constants:
            continue
        if relative in STORAGE_MODULES_WITHOUT_OWN_FILE:
            continue
        names = _json_names(constants)
        if not names or names - backed_up:
            missing[relative] = names - backed_up
    return missing


def test_every_storage_file_is_in_the_backup_list() -> None:
    """No module writes a ``.storage`` file that ``HISTORY_FILES`` does not name."""
    assert unbacked_storage_files(_modules(), set(HISTORY_FILES)) == {}


def test_every_hsem_json_file_name_is_in_the_backup_list() -> None:
    """An ``hsem_*.json`` name anywhere in the source is a backed-up file."""
    known = {*HISTORY_FILES, PENDING_IMPORT_FILE}
    stray = {
        relative: names
        for relative, constants in _modules().items()
        if (
            names := {
                name
                for name in _json_names(constants)
                if name.startswith("hsem_") and name not in known
            }
        )
    }
    assert stray == {}


def test_every_backup_list_name_is_written_by_a_module() -> None:
    """The backup list holds no name that no module uses."""
    used = set().union(*(_json_names(c) for c in _modules().values()))
    assert set(HISTORY_FILES) <= used
    assert len(set(HISTORY_FILES)) == len(HISTORY_FILES)


def test_only_the_dashboard_uses_the_home_assistant_store() -> None:
    """A new ``Store`` would be a ``.storage`` file outside the backup."""
    users = {
        path.relative_to(INTEGRATION).as_posix()
        for path in sorted(INTEGRATION.rglob("*.py"))
        if "homeassistant.helpers.storage" in path.read_text(encoding="utf-8")
    }
    assert users == STORE_MODULES


def test_the_guard_reports_a_file_missing_from_the_backup_list() -> None:
    """A store that is not in the list fails the guard, as do built names."""
    modules = {
        "coordinator_new.py": {".storage", "hsem_new_history.json"},
        "coordinator_built.py": {".storage"},
        "coordinator_old.py": {".storage", "hsem_savings_history.json"},
        "services_backup.py": {".storage"},
        "other.py": {"manifest.json"},
    }

    assert unbacked_storage_files(modules, set(HISTORY_FILES)) == {
        "coordinator_new.py": {"hsem_new_history.json"},
        "coordinator_built.py": set(),
    }


def test_the_guard_fails_without_the_files_added_for_issue_1447() -> None:
    """With the list of schema version 1, the four newer stores are reported."""
    version_1 = set(HISTORY_FILES[:5])

    missing = unbacked_storage_files(_modules(), version_1)

    assert set().union(*missing.values()) == {
        "hsem_battery_efficiency.json",
        "hsem_energy_history.json",
        "hsem_price_history.json",
        "hsem_what_if_history.json",
    }
