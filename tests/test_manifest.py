"""Tests for manifest.json's declared requirements.

Home Assistant only pip-installs packages listed in a component's
``manifest.json`` ``requirements`` array (see ``Integration.requirements`` in
HA core). The repo-root ``requirements.txt`` / ``pyproject.toml`` only pin
dependencies for this repo's own dev/CI tooling and are never consulted by a
real HA instance. The MILP optimiser (``planner/milp_optimizer.py``) needs
scipy/numpy at runtime, so they must be declared here or MILP silently never
activates for HACS users (issue #876).

The requirements MUST be version ranges, not exact pins. Home Assistant core
hard-pins numpy in its own ``homeassistant/package_constraints.txt`` (applied
as a pip ``--constraint`` to every custom component install) and bumps that
pin every few HA releases. An exact ``numpy==X`` pin here collides with
whatever HA core currently pins and breaks setup for every user the moment
the two diverge — this happened in production right after #876 shipped with
an exact pin. A range that comfortably contains HA's current pin lets the
resolver satisfy both without needing to track HA's release cadence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.version import Version

MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hsem"
    / "manifest.json"
)

# Home Assistant core's package_constraints.txt has pinned numpy to 2.2.x/2.3.x
# since 2025.6; verified directly against the constraints file for several
# recent HA releases (2025.1 -> 2026.8). Our range must contain this pin.
HA_CONSTRAINED_NUMPY_VERSION = Version("2.3.2")


def _load_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(MANIFEST_PATH.read_text())
    return manifest


def _requirement_specs() -> dict[str, Requirement]:
    manifest = _load_manifest()
    requirements = [Requirement(req) for req in manifest["requirements"]]
    return {req.name: req for req in requirements}


def test_manifest_declares_scipy_and_numpy_requirements():
    """MILP needs scipy/numpy; HA only installs what's in `requirements`."""
    requirements = _requirement_specs()

    assert "scipy" in requirements, (
        "manifest.json must declare scipy in 'requirements' so Home Assistant "
        "installs it for HACS users — MILP silently falls back otherwise"
    )
    assert "numpy" in requirements, (
        "manifest.json must declare numpy in 'requirements' so Home Assistant "
        "installs it for HACS users"
    )


def test_manifest_requirements_are_ranges_not_exact_pins():
    """Exact pins collide with HA core's own numpy pin; ranges must be used."""
    requirements = _requirement_specs()

    for name, req in requirements.items():
        specifier_strs = {str(spec) for spec in req.specifier}
        assert not any(s.startswith("==") for s in specifier_strs), (
            f"manifest.json pins '{name}' to an exact version ({req}). "
            "Home Assistant core hard-pins numpy in its own "
            "package_constraints.txt and bumps it every few releases — an "
            "exact pin here will conflict with HA's pin and break setup for "
            "every user. Use a range instead (see module docstring)."
        )


def test_manifest_numpy_range_contains_ha_core_constrained_version():
    """The declared numpy range must accept the version HA core currently pins."""
    requirements = _requirement_specs()

    assert requirements["numpy"].specifier.contains(HA_CONSTRAINED_NUMPY_VERSION), (
        f"manifest.json's numpy requirement ({requirements['numpy']}) does not "
        f"accept numpy=={HA_CONSTRAINED_NUMPY_VERSION}, which is what Home "
        "Assistant core currently pins in package_constraints.txt — installs "
        "will fail for every user."
    )


def test_manifest_requirement_ranges_contain_repo_dev_pins():
    """The manifest ranges must accept the exact versions used for dev/CI."""
    requirements = _requirement_specs()

    repo_requirements_path = Path(__file__).resolve().parent.parent / "requirements.txt"
    repo_pins = {
        line.split("==")[0]: Version(line.split("==")[1])
        for line in repo_requirements_path.read_text().splitlines()
        if "==" in line
    }

    assert requirements["scipy"].specifier.contains(repo_pins["scipy"])
    assert requirements["numpy"].specifier.contains(repo_pins["numpy"])
