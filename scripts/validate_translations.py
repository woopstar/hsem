#!/usr/bin/env python3
"""Validate HSEM translation files against the English source of truth.

``translations/en.json`` is the source of truth for every user-facing string
(config/options flow steps, selectors, entity names, service definitions).
Every other language file must have exactly the same set of keys, and no
value should be left as an unmodified copy of the English text unless it is
a genuine cognate (see ``_is_number`` and the untranslated-value check below).

Checks performed per target language:

- Missing keys: present in en.json, absent in the target file.
- Stale keys: present in the target file, absent in en.json (usually a
  leftover from a removed/renamed feature).
- Untranslated values: the target value is byte-for-byte identical to the
  English value. Flagged as a warning (not an error) because short cognates
  ("November" / "november", acronyms like "OCPP", "SoC") are legitimately
  identical across languages. Longer strings that match are almost always a
  missed translation.
- Placeholder mismatch: ``{placeholder}`` tokens in the value must match
  exactly between English and the target language (order-independent).

Exit code is non-zero if any missing key, stale key, or placeholder
mismatch is found. Untranslated-value warnings do not fail the build --
they are printed for manual review, since some are legitimate cognates.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TRANSLATIONS_DIR = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "hsem"
    / "translations"
)
SOURCE_LANGUAGE = "en"
TARGET_LANGUAGES = ["da", "de", "es"]

# Strings that are allowed to be identical to the English source without
# being flagged as untranslated: numbers-only values, and known cognates
# that are spelled the same across all four supported languages.
_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z0-9_]+\}")


def _is_number(value: str) -> bool:
    return bool(re.fullmatch(r"[\d.,%\s-]+", value))


def flatten(data: dict, prefix: str = "") -> dict[str, str]:
    """Flatten a nested translation dict into ``dotted.key -> value`` pairs."""
    items: dict[str, str] = {}
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            items.update(flatten(value, full_key))
        else:
            items[full_key] = value
    return items


def load(language: str) -> dict[str, str]:
    path = TRANSLATIONS_DIR / f"{language}.json"
    with path.open(encoding="utf-8") as f:
        return flatten(json.load(f))


def main() -> int:
    source = load(SOURCE_LANGUAGE)
    errors = 0
    warnings = 0

    for lang in TARGET_LANGUAGES:
        path = TRANSLATIONS_DIR / f"{lang}.json"
        if not path.exists():
            print(f"ERR  {lang}.json does not exist")
            errors += 1
            continue

        target = load(lang)

        missing = sorted(set(source) - set(target))
        stale = sorted(set(target) - set(source))
        untranslated = []
        placeholder_mismatch = []

        for key in sorted(set(source) & set(target)):
            en_value = source[key]
            target_value = target[key]
            if not isinstance(en_value, str) or not isinstance(target_value, str):
                continue

            en_placeholders = set(_PLACEHOLDER_RE.findall(en_value))
            target_placeholders = set(_PLACEHOLDER_RE.findall(target_value))
            if en_placeholders != target_placeholders:
                placeholder_mismatch.append((key, en_placeholders, target_placeholders))

            if (
                en_value == target_value
                and len(en_value) > 2
                and not _is_number(en_value)
            ):
                untranslated.append(key)

        print(f"=== {lang}.json ===")
        print(f"  missing keys:          {len(missing)}")
        print(f"  stale keys:            {len(stale)}")
        print(f"  placeholder mismatches:{len(placeholder_mismatch)}")
        print(f"  untranslated (warn):   {len(untranslated)}")

        for key in missing:
            print(f"  MISSING  {key} = {source[key]!r}")
        for key in stale:
            print(f"  STALE    {key} = {target[key]!r}")
        for key, en_ph, target_ph in placeholder_mismatch:
            print(
                f"  PLACEHOLDER  {key}: en={sorted(en_ph)} {lang}={sorted(target_ph)}"
            )
        for key in untranslated:
            print(f"  UNTRANSLATED  {key} = {source[key]!r}")

        errors += len(missing) + len(stale) + len(placeholder_mismatch)
        warnings += len(untranslated)
        print()

    print(f"Total errors: {errors}, warnings (review): {warnings}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
