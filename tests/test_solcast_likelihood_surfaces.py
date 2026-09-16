"""Cross-surface tests for the Solcast likelihood option list (issue #1023).

The option list was hardcoded as a literal in three places plus four copies of
its default value.  All of them now derive from
:mod:`custom_components.hsem.utils.solcast_likelihood`, and these tests assert
that every surface — including ``translations/en.json`` — still agrees, so a
future percentile cannot be added to one surface and missed on another.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.hsem.const import DEFAULT_CONFIG_VALUES
from custom_components.hsem.custom_selectors.solcast_likelihood import (
    _DEFAULT,
    _OPTIONS,
)
from custom_components.hsem.models.sensor_config import SensorConfig
from custom_components.hsem.select import SELECTOR_DESCRIPTIONS
from custom_components.hsem.utils.sensornames.diagnostics import (
    get_solcast_likelihood_selector_key,
)
from custom_components.hsem.utils.solcast_likelihood import (
    DEFAULT_SOLCAST_LIKELIHOOD,
    SOLCAST_LIKELIHOOD_OPTIONS,
    SolcastLikelihood,
)

_CONFIG_KEY = "hsem_solcast_pv_forecast_forecast_likelihood"
_COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / "hsem"


def _en_selector_options() -> list[str]:
    """Return the likelihood options declared in en.json's selector block."""
    raw = (_COMPONENT_DIR / "translations" / "en.json").read_text(encoding="utf-8")
    return list(json.loads(raw)["selector"]["pv_estimate_likelihood"]["options"])


class TestSolcastLikelihoodSurfacesAgree:
    """Every surface that enumerates the likelihood options must match."""

    def test_canonical_order_is_preserved(self) -> None:
        """The user-visible order must not change (P50, P10, P90)."""
        assert SOLCAST_LIKELIHOOD_OPTIONS == (
            "pv_estimate",
            "pv_estimate10",
            "pv_estimate90",
        )

    def test_options_cover_every_enum_member(self) -> None:
        assert set(SOLCAST_LIKELIHOOD_OPTIONS) == {m.value for m in SolcastLikelihood}

    def test_selector_entity_matches(self) -> None:
        assert list(SOLCAST_LIKELIHOOD_OPTIONS) == _OPTIONS

    def test_select_platform_matches(self) -> None:
        description = next(
            d
            for d in SELECTOR_DESCRIPTIONS
            if d.key == get_solcast_likelihood_selector_key()
        )
        assert description.options == list(SOLCAST_LIKELIHOOD_OPTIONS)

    def test_config_flow_step_matches(self) -> None:
        """The flow step's selector literal must be gone, not merely equal."""
        raw = (_COMPONENT_DIR / "flows" / "solcast.py").read_text(encoding="utf-8")
        assert '"pv_estimate", "pv_estimate10", "pv_estimate90"' not in raw
        assert "SOLCAST_LIKELIHOOD_OPTIONS" in raw

    def test_translations_match(self) -> None:
        assert sorted(_en_selector_options()) == sorted(SOLCAST_LIKELIHOOD_OPTIONS)


class TestSolcastLikelihoodDefault:
    """The default percentile must be consistent across all four definitions."""

    def test_default_is_a_valid_option(self) -> None:
        assert DEFAULT_SOLCAST_LIKELIHOOD in SOLCAST_LIKELIHOOD_OPTIONS

    def test_default_is_the_median_estimate(self) -> None:
        assert SolcastLikelihood.Estimate50.value == DEFAULT_SOLCAST_LIKELIHOOD

    def test_config_entry_default_matches(self) -> None:
        assert DEFAULT_CONFIG_VALUES[_CONFIG_KEY] == DEFAULT_SOLCAST_LIKELIHOOD

    def test_sensor_config_default_matches(self) -> None:
        config = SensorConfig()
        assert (
            config.solcast_pv_forecast_forecast_likelihood == DEFAULT_SOLCAST_LIKELIHOOD
        )

    def test_selector_entity_default_matches(self) -> None:
        assert _DEFAULT == DEFAULT_SOLCAST_LIKELIHOOD


class TestSolcastLikelihoodValues:
    """The values are upstream Solcast attribute keys and must not be renamed."""

    @pytest.mark.parametrize(
        ("member", "expected"),
        [
            (SolcastLikelihood.Estimate50, "pv_estimate"),
            (SolcastLikelihood.Estimate10, "pv_estimate10"),
            (SolcastLikelihood.Estimate90, "pv_estimate90"),
        ],
    )
    def test_value_is_the_upstream_attribute_key(
        self, member: SolcastLikelihood, expected: str
    ) -> None:
        assert member.value == expected

    def test_str_enum_compares_equal_to_raw_string(self) -> None:
        """StrEnum keeps persisted config values working without migration."""
        assert SolcastLikelihood.Estimate10 == "pv_estimate10"
