"""Solcast PV forecast likelihood enumeration for HSEM.

Defines the Solcast forecast percentile HSEM can read. The values are the
attribute keys published by the Solcast integration on its forecast sensors —
they are dictated upstream, not by HSEM, so they must not be renamed here.

Usage
-----
>>> from custom_components.hsem.utils.solcast_likelihood import SolcastLikelihood
>>> cfg.solcast_pv_forecast_forecast_likelihood = SolcastLikelihood.Estimate50.value
"""

from enum import StrEnum


class SolcastLikelihood(StrEnum):
    """Solcast forecast percentiles selectable as HSEM's PV forecast source."""

    Estimate50 = "pv_estimate"
    """Median (P50) forecast — Solcast's central estimate."""

    Estimate10 = "pv_estimate10"
    """P10 forecast — pessimistic; only 10% of outcomes fall below it."""

    Estimate90 = "pv_estimate90"
    """P90 forecast — optimistic; 90% of outcomes fall below it."""


SOLCAST_LIKELIHOOD_OPTIONS: tuple[str, ...] = tuple(
    member.value for member in SolcastLikelihood
)
"""Selectable likelihood values, in the order shown to the user.

Import this everywhere a user-facing surface enumerates the options — the
``select`` platform, :class:`~custom_selectors.solcast_likelihood.HSEMSolcastLikelihoodSelector`,
and the config/options flow step — so the surfaces cannot drift apart.  The
order follows the enum's declaration order and is asserted against
``translations/en.json`` in the tests.
"""

DEFAULT_SOLCAST_LIKELIHOOD: str = SolcastLikelihood.Estimate50.value
"""Default forecast percentile: Solcast's median estimate.

Used as the config-entry default, the selector's initial value, and the
fallback when the stored option is missing or empty.
"""
