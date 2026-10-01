"""Tests for the installation tag of backtest files (issue #1225)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.backtest.site import (
    SITE_ENV,
    SITE_KEY,
    describe_site,
    require_same_site,
    same_site,
    site_of,
    validate_site,
)


class TestValidateSite:
    @pytest.mark.parametrize("tag", ["a", "site-a", "home-2", "x" * 32, "7"])
    def test_a_short_plain_label_is_accepted(self, tag: str) -> None:
        assert validate_site(tag) == tag

    @pytest.mark.parametrize("tag", [None, ""])
    def test_nothing_means_no_tag(self, tag: str | None) -> None:
        assert validate_site(tag) is None

    @pytest.mark.parametrize(
        "tag",
        [
            "Site-A",
            "my house",
            "sensor.batteries_soc",
            "someone@example.org",
            "192.168.1.10",
            "-site",
            "site-",
            "x" * 33,
            15,
        ],
    )
    def test_anything_that_could_identify_a_home_is_refused(self, tag: Any) -> None:
        with pytest.raises(ValueError, match="invalid site tag"):
            validate_site(tag)


class TestSiteOf:
    def test_reads_the_top_level_key(self) -> None:
        assert site_of({SITE_KEY: "site-a", "planner_input": {}}) == "site-a"

    def test_reads_a_diagnostics_download(self) -> None:
        assert site_of({"data": {SITE_KEY: "site-b"}}) == "site-b"

    def test_a_file_without_a_tag_has_none(self) -> None:
        assert site_of({"planner_input": {}}) is None
        assert site_of({"data": {"planner_input": {}}}) is None
        assert site_of({"data": "not a mapping"}) is None

    def test_an_invalid_tag_in_a_file_is_refused(self) -> None:
        with pytest.raises(ValueError, match="invalid site tag"):
            site_of({SITE_KEY: "Elm Street 4"})


class TestPairing:
    def test_equal_tags_pair(self) -> None:
        assert same_site("site-a", "site-a")

    def test_different_tags_do_not_pair(self) -> None:
        assert not same_site("site-a", "site-b")

    def test_two_untagged_files_pair(self) -> None:
        """A private collection from before the tag keeps working."""
        assert same_site(None, None)

    def test_an_untagged_file_never_pairs_with_a_tagged_one(self) -> None:
        assert not same_site(None, "site-a")
        assert not same_site("site-a", None)

    def test_require_same_site_passes_for_a_pair(self) -> None:
        require_same_site("site-a", "site-a", "cycle and actuals")
        require_same_site(None, None, "cycle and actuals")

    def test_require_same_site_names_both_sides(self) -> None:
        with pytest.raises(ValueError) as err:
            require_same_site("site-a", None, "cycle x.json and its actuals")
        assert str(err.value) == (
            "cycle x.json and its actuals: site 'site-a' and no site tag are not "
            "the same installation"
        )

    def test_describe_site(self) -> None:
        assert describe_site("site-a") == "site 'site-a'"
        assert describe_site(None) == "no site tag"

    def test_the_environment_variable_name(self) -> None:
        assert SITE_ENV == "HSEM_BACKTEST_SITE"
