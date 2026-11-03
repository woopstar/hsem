"""Tests for the metadata the Home Assistant Marketplace reads (issue #1354).

Home Assistant 2026.11 ships HACS inside core as the Marketplace. Before it
installs a release it downloads ``hacs.json`` from the release tag and refuses
the release when ``homeassistant`` names a newer core than the user runs. After
writing the files it checks that ``manifest.json`` carries the domain and a
version the loader accepts. The HACS action's ``brands`` check accepts a
``brand/icon.png`` inside the integration folder, and its
``integration_categories`` check wants one to three known categories.

These tests pin the keys and files those checks read so a release cannot ship
without them.
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path
from typing import Any

from awesomeversion import AwesomeVersion, AwesomeVersionStrategy
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "hsem"
HACS_JSON_PATH = REPO_ROOT / "hacs.json"
MANIFEST_PATH = INTEGRATION_DIR / "manifest.json"
BRAND_DIR = INTEGRATION_DIR / "brand"

# The ``energy`` family of integration categories, copied from
# https://contributors.home-assistant.io/core/integrations/categories/ on
# 2026-11-03. HSEM manages a solar battery and EV charging, so its categories
# must come from here; a value outside the published list fails the HACS
# ``integration_categories`` check.
ENERGY_CATEGORIES = frozenset(
    {
        "energy",
        "energy.backup_power",
        "energy.energy_prices",
        "energy.ev_charging",
        "energy.meters_monitoring",
        "energy.solar_batteries",
    }
)
# hassfest's manifest schema for ``categories``.
CATEGORY_PATTERN = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)?$")

# The version formats Home Assistant's loader accepts for a custom integration
# (``LOADABLE_VERSION_STRATEGIES`` in the Marketplace's integration repository).
LOADABLE_VERSION_STRATEGIES = [
    AwesomeVersionStrategy.CALVER,
    AwesomeVersionStrategy.SEMVER,
    AwesomeVersionStrategy.SIMPLEVER,
    AwesomeVersionStrategy.BUILDVER,
    AwesomeVersionStrategy.PEP440,
]

# ``utils/schema.py`` relies on the probatio alias Home Assistant 2026.10
# introduced, so no release from ``main`` can load on an older core.
OLDEST_SUPPORTED_HOMEASSISTANT = Version("2026.10.0")


def _load_json(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def _pinned_homeassistant_version() -> Version:
    for line in (REPO_ROOT / "requirements.txt").read_text().splitlines():
        name, sep, version = line.partition("==")
        if sep and name.strip() == "homeassistant":
            return Version(version.strip())
    raise AssertionError("requirements.txt does not pin homeassistant")


def _png_size(path: Path) -> tuple[int, int]:
    header = path.read_bytes()[:24]
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG file"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


class TestHacsJson:
    """``hacs.json`` is the file the Marketplace reads per release."""

    def test_declares_the_keys_the_marketplace_reads(self) -> None:
        hacs = _load_json(HACS_JSON_PATH)

        assert isinstance(hacs.get("name"), str) and hacs["name"]
        assert hacs.get("zip_release") is True
        assert hacs.get("filename") == "hsem.zip", (
            "release.yml uploads hsem.zip; hacs.json must name the same asset"
        )
        assert isinstance(hacs.get("homeassistant"), str)

    def test_minimum_homeassistant_matches_what_the_code_needs(self) -> None:
        minimum = Version(_load_json(HACS_JSON_PATH)["homeassistant"])

        assert minimum >= OLDEST_SUPPORTED_HOMEASSISTANT, (
            f"hacs.json offers releases to Home Assistant {minimum}, but "
            "utils/schema.py needs the probatio alias that arrived in "
            f"{OLDEST_SUPPORTED_HOMEASSISTANT}; such a release cannot load"
        )
        assert minimum <= _pinned_homeassistant_version(), (
            "hacs.json demands a newer Home Assistant than CI tests against"
        )

    def test_minimum_is_a_comparable_version(self) -> None:
        """The Marketplace compares ``homeassistant`` with AwesomeVersion."""
        minimum = AwesomeVersion(_load_json(HACS_JSON_PATH)["homeassistant"])

        assert minimum.strategy == AwesomeVersionStrategy.CALVER


class TestManifest:
    """``manifest.json`` keys the Marketplace and the HACS action check."""

    def test_declares_the_required_keys(self) -> None:
        manifest = _load_json(MANIFEST_PATH)

        for key in ("domain", "name", "version", "documentation", "issue_tracker"):
            assert isinstance(manifest.get(key), str) and manifest[key], key
        assert isinstance(manifest.get("codeowners"), list) and manifest["codeowners"]
        assert manifest["domain"] == "hsem"
        assert re.fullmatch(r"[a-z0-9_-]+", manifest["domain"])

    def test_version_is_loadable(self) -> None:
        """A manifest version the loader rejects rolls the install back."""
        version = _load_json(MANIFEST_PATH)["version"]

        AwesomeVersion(version, ensure_strategy=LOADABLE_VERSION_STRATEGIES)

    def test_categories_are_valid(self) -> None:
        categories = _load_json(MANIFEST_PATH).get("categories")

        assert isinstance(categories, list), (
            "manifest.json needs 'categories' or the HACS "
            "integration_categories check warns"
        )
        assert 1 <= len(categories) <= 3
        assert len(set(categories)) == len(categories), "duplicate categories"
        for category in categories:
            assert CATEGORY_PATTERN.fullmatch(category), category
            assert category in ENERGY_CATEGORIES, category
            parent, _, subcategory = category.partition(".")
            if subcategory:
                assert parent not in categories, (
                    f"hassfest rejects '{parent}' next to '{category}'"
                )


class TestBrand:
    """The HACS ``brands`` check accepts ``brand/icon.png`` in the integration."""

    def test_icon_ships_inside_the_integration_folder(self) -> None:
        icon = BRAND_DIR / "icon.png"

        assert icon.is_file(), (
            "custom_components/hsem/brand/icon.png is missing; the release ZIP "
            "is built from custom_components/hsem, so it must live there"
        )
        assert _png_size(icon) == (256, 256)

    def test_retina_icon_matches_the_brands_repository(self) -> None:
        icon_2x = BRAND_DIR / "icon@2x.png"

        assert icon_2x.is_file()
        assert _png_size(icon_2x) == (512, 512)
