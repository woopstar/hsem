"""Which installation a recorded file is from (issue #1225).

A planner cycle and an actuals file describe one installation each.  Comparing
a plan with what happened only means something when both are from the same
one: another house's meters turn every difference into a fake forecast error.
Nothing in a planner input or in a history export says where it was recorded,
so the files carry a **site tag**: a short label chosen by whoever collects
(``HSEM_BACKTEST_SITE`` in ``.env``), written by the harvest and by
``scripts/build_actuals.py`` as the top-level ``site`` key.

Two files pair only when their tags are equal.  A file without a tag has the
tag ``None``: two untagged files still pair, so a private collection from
before the tag existed keeps working, but an untagged file never pairs with a
tagged one.

The tag is not an identity.  It only has to tell the installations of one
collection apart, and it ends up in a public repository, so it is restricted
to lower-case letters, digits and hyphens: no name, address, host or entity id
fits by accident.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

__all__ = [
    "SITE_ENV",
    "SITE_KEY",
    "describe_site",
    "require_same_site",
    "same_site",
    "site_of",
    "validate_site",
]

#: Top-level key of a committed cycle and of an actuals file.
SITE_KEY = "site"

#: Environment variable the collection scripts read the tag from.
SITE_ENV = "HSEM_BACKTEST_SITE"

_SITE_TAG = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")


def validate_site(tag: str | None) -> str | None:
    """Return *tag* when it is a usable site tag, ``None`` for no tag.

    Args:
        tag: The label to check; ``None`` and the empty string mean no tag.

    Returns:
        The tag, or ``None``.

    Raises:
        ValueError: If the tag is not 1-32 lower-case letters, digits and
            hyphens, starting and ending with a letter or digit.
    """
    if tag is None or tag == "":
        return None
    if not isinstance(tag, str) or _SITE_TAG.fullmatch(tag) is None:
        raise ValueError(
            f"invalid site tag {tag!r}: use 1-32 lower-case letters, digits and "
            f"hyphens, for example 'site-a'"
        )
    return tag


def site_of(document: Mapping[str, Any]) -> str | None:
    """Return the site tag of a loaded cycle or actuals file.

    A Home Assistant diagnostics download nests the payload under ``data``;
    the tag is looked up on the document itself first and then there.

    Args:
        document: The decoded JSON document.

    Returns:
        The tag, or ``None`` when the file carries none.

    Raises:
        ValueError: If the file carries a tag that is not a valid one.
    """
    tag = document.get(SITE_KEY)
    if tag is None:
        nested = document.get("data")
        if isinstance(nested, Mapping):
            tag = nested.get(SITE_KEY)
    return validate_site(tag)


def same_site(left: str | None, right: str | None) -> bool:
    """Return whether two tags name the same installation.

    Two untagged files pair; an untagged file and a tagged one do not.

    Args:
        left: One file's tag.
        right: The other file's tag.

    Returns:
        ``True`` when the tags are equal.
    """
    return left == right


def describe_site(tag: str | None) -> str:
    """Render a tag for a message."""
    return f"site {tag!r}" if tag is not None else "no site tag"


def require_same_site(left: str | None, right: str | None, what: str) -> None:
    """Refuse to compare files of different installations.

    Args:
        left: One file's tag.
        right: The other file's tag.
        what: What is being compared, for the message.

    Raises:
        ValueError: If the tags differ.
    """
    if not same_site(left, right):
        raise ValueError(
            f"{what}: {describe_site(left)} and {describe_site(right)} are not "
            f"the same installation"
        )
