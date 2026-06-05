"""Tests for version comparison logic in the HSEM __init__ module.

Covers:
- Correct numeric ordering (1.10 > 1.9)
- Pre-release version ordering (1.5.0a1 < 1.5.0)
- Patch version ordering (1.10.0 < 1.10.1)
- Invalid version strings are handled without raising
"""

from packaging.version import Version

from custom_components.hsem import _parse_version


class TestParseVersion:
    """Tests for the _parse_version helper."""

    def test_returns_version_object_for_valid_string(self):
        """A valid version string should return a packaging.version.Version."""
        result = _parse_version("1.9.0")
        assert isinstance(result, Version)

    def test_returns_none_for_invalid_version(self):
        """An invalid version string should return None instead of raising."""
        result = _parse_version("not-a-version")
        assert result is None

    def test_returns_none_for_empty_string(self):
        """An empty string is not a valid version and should return None."""
        result = _parse_version("")
        assert result is None

    def test_returns_none_for_bare_text(self):
        """Arbitrary text without digits should return None."""
        result = _parse_version("release-candidate")
        assert result is None

    def test_parses_pre_release_version(self):
        """Pre-release versions (PEP 440) must be parsed successfully."""
        result = _parse_version("1.5.0a1")
        assert result is not None
        assert result == Version("1.5.0a1")


def _v(version_str: str) -> Version:
    """Parse a version string and assert it is not None.

    Helper that wraps :func:`_parse_version` for ordering tests where the
    input is always a valid PEP 440 string.  The assertion both validates the
    assumption and narrows the ``Version | None`` return type for Pyright.
    """
    result = _parse_version(version_str)
    assert result is not None, f"Expected valid version, got None for {version_str!r}"
    return result


class TestVersionOrdering:
    """Tests for numeric version ordering correctness.

    String comparison produces wrong results, e.g. "1.10" < "1.9" because
    "1" == "1" and then "." == "." and then "1" < "9" lexicographically.
    packaging.version.Version must produce the correct numeric result.
    """

    def test_1_10_greater_than_1_9(self):
        """1.10 must compare as greater than 1.9 (not less, as string comparison gives)."""
        assert _v("1.10") > _v("1.9")

    def test_1_9_less_than_1_10(self):
        """1.9 must compare as less than 1.10."""
        assert _v("1.9") < _v("1.10")

    def test_1_10_1_greater_than_1_10(self):
        """1.10.1 must compare as greater than 1.10."""
        assert _v("1.10.1") > _v("1.10")

    def test_1_10_less_than_1_10_1(self):
        """1.10 must compare as less than 1.10.1."""
        assert _v("1.10") < _v("1.10.1")

    def test_equal_versions(self):
        """Two identical version strings must compare as equal."""
        assert _v("1.10") == _v("1.10")

    def test_pre_release_less_than_release(self):
        """A pre-release version (1.5.0a1) must compare as less than the release (1.5.0)."""
        assert _v("1.5.0a1") < _v("1.5.0")

    def test_min_huawei_version_accepted(self):
        """Installed version equal to the minimum required version must be accepted."""
        installed = _v("1.5.0a1")
        required = _v("1.5.0a1")
        assert installed >= required

    def test_version_above_minimum_accepted(self):
        """A version higher than the minimum required must be accepted."""
        installed = _v("1.10.0")
        required = _v("1.5.0a1")
        assert installed >= required

    def test_version_below_minimum_rejected(self):
        """A version lower than the minimum required must be rejected."""
        installed = _v("1.4.9")
        required = _v("1.5.0a1")
        assert installed < required
