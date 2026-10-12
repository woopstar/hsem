"""Load a standalone script from ``scripts/`` as a module.

``scripts/`` is not a package, so its checkers and CLI helpers cannot be
imported by name; the tests of each script load it from its path instead.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str) -> ModuleType:
    """Load ``scripts/<name>.py`` and register it in ``sys.modules``.

    Args:
        name: The script's file name without the ``.py`` suffix.

    Returns:
        The executed module.
    """
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
