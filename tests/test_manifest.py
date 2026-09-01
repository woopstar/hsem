"""Tests for manifest.json's declared requirements.

Home Assistant only pip-installs packages listed in a component's
``manifest.json`` ``requirements`` array (see ``Integration.requirements`` in
HA core). The repo-root ``requirements.txt`` / ``pyproject.toml`` only pin
dependencies for this repo's own dev/CI tooling and are never consulted by a
real HA instance. The MILP optimiser (``planner/milp_optimizer.py``) needs
scipy/numpy at runtime, so they must be declared here or MILP silently never
activates for HACS users (issue #876).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hsem"
    / "manifest.json"
)


def _load_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text())
    return manifest


def test_manifest_declares_scipy_and_numpy_requirements():
    """MILP needs scipy/numpy; HA only installs what's in `requirements`."""
    manifest = _load_manifest()
    requirements = manifest.get("requirements", [])

    assert any(req.startswith("scipy") for req in requirements), (
        "manifest.json must declare scipy in 'requirements' so Home Assistant "
        "installs it for HACS users — MILP silently falls back otherwise"
    )
    assert any(req.startswith("numpy") for req in requirements), (
        "manifest.json must declare numpy in 'requirements' so Home Assistant "
        "installs it for HACS users"
    )


def test_manifest_requirement_pins_match_repo_dev_pins():
    """The manifest pins must stay in sync with requirements.txt's dev pins."""
    manifest = _load_manifest()
    requirements = {req.split("==")[0]: req for req in manifest["requirements"]}

    repo_requirements_path = Path(__file__).resolve().parent.parent / "requirements.txt"
    repo_pins = {
        line.split("==")[0]: line
        for line in repo_requirements_path.read_text().splitlines()
        if "==" in line
    }

    assert requirements["scipy"] == repo_pins["scipy"]
    assert requirements["numpy"] == repo_pins["numpy"]
