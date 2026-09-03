---
name: hsem-translation-sync
description: Activate before opening or updating a pull request, and whenever a user-facing string (entity name, config/options flow label, selector option, error, service) is added, changed, or removed. Keeps translations/{en,da,de,es}.json in sync.
---

# HSEM Translation Sync — Keep en/da/de/es in Sync

HSEM ships four languages: **English** (`en.json`, source of truth), **Danish**
(`da.json`), **German** (`de.json`), and **Spanish** (`es.json`), all under
`custom_components/hsem/translations/`. Every user-facing string — config/options
flow step titles, field labels, field descriptions, error/abort messages, selector
option labels, entity names, service names/descriptions — must exist and be
genuinely translated in all four files.

Activate this skill:

- Before opening a new pull request (as part of `hsem-pr-workflow`)
- Before updating an open PR after a follow-up commit that touches
  `translations/en.json`, `config_flow.py`, `options_flow.py`, any `flows/*.py`,
  or any file wiring an entity's `translation_key`
- Any time you add, rename, or remove a config/options flow field, selector,
  entity `translation_key`, error key, abort reason, or service

## Step 1 — Run the validator

```bash
python3 scripts/validate_translations.py
```

This diffs `da.json`, `de.json`, `es.json` against `en.json` (the source of
truth) and reports, per language:

- **Missing keys** — present in English, absent in the target file. Hard error.
- **Stale keys** — present in the target file, absent in English (leftover from
  a removed/renamed field). Hard error.
- **Placeholder mismatches** — `{placeholder}` tokens differ between English and
  the target value. Hard error.
- **Untranslated values (warning)** — the target value is byte-identical to the
  English value. Not auto-failed, because short cognates are legitimate
  (`"OCPP"`, `"SoC"`, `"Auto"`, and a few month names are spelled the same in
  Danish/German/English). Review each one: if it's a multi-word phrase or a
  sentence, it's almost always a missed translation, not a cognate.

The script exits non-zero if there are any hard errors.

## Step 2 — Fix what the validator finds

- **Missing keys**: translate the English value into the target language. Check
  whether the same field text already exists translated elsewhere in the file
  first — `config.step.X` and `options.step.X` frequently duplicate the exact
  same field label/description, and `options.step.ev_second_planned_load`
  duplicates `options.step.ev_planned_load` with an "EV 2" prefix. Reuse the
  existing translation rather than re-translating from scratch; this is the
  fastest way to fix a batch of missing keys and keeps terminology consistent.
- **Stale keys**: confirm the key has no corresponding code (`grep -rn
"<leaf_key>" custom_components/hsem`). If genuinely dead, delete it. If it's a
  key that's about to be reintroduced, leave a note in the PR instead of
  deleting.
- **Placeholder mismatches**: the target value must contain exactly the same
  `{placeholder}` tokens as English, in whatever order reads naturally in that
  language. Never drop or rename a placeholder.
- **Untranslated warnings**: translate the ones that are real sentences/phrases.
  Leave single-word cognates and acronyms as-is (do not force a translation
  that doesn't exist, e.g. "Server", "Port", "SoC", "OCPP").

## Step 3 — Terminology glossary

Use this glossary consistently across all three target languages so the same
English concept always renders the same way, instead of drifting between PRs:

| English                 | Danish                      | German                     | Spanish                          |
| ----------------------- | --------------------------- | -------------------------- | -------------------------------- |
| State of Charge (SoC)   | SoC (keep)                  | SoC (keep)                 | SoC (keep)                       |
| Working Mode            | Arbejdstilstand             | Arbeitsmodus               | Modo de funcionamiento           |
| Planner                 | Planlægger                  | Planer                     | Planificador                     |
| Grid charge             | Netopladning                | Netzladung                 | Carga desde la red               |
| Self-consumption        | Selvforbrug                 | Eigenverbrauch             | Autoconsumo                      |
| Export / feed-in        | Eksport                     | Einspeisung / Export       | Exportación                      |
| Wait mode               | Ventetilstand               | Wartemodus                 | Modo de espera                   |
| Hysteresis              | Hysterese                   | Hysterese                  | Histéresis                       |
| Phase-aware charging    | Fasebevidst opladning       | Phasenbewusstes Laden      | Carga con reconocimiento de fase |
| Fuse                    | Sikring                     | Sicherung                  | Fusible                          |
| Charge Point / CPID     | Ladepunkt                   | Ladepunkt                  | Punto de carga                   |
| EV charger              | EV-lader                    | EV-Ladegerät / Ladestation | Cargador de VE                   |
| Degraded mode           | Degraderet tilstand         | Eingeschränkter Modus      | Modo degradado                   |
| Read-only mode          | Skrivebeskyttet tilstand    | Nur-Lese-Modus             | Modo de solo lectura             |
| Force mode              | Tvungen tilstand            | Erzwungener Modus          | Modo forzado                     |
| Hardware writes         | Hardwareskrivninger         | Hardware-Schreibvorgänge   | Escrituras de hardware           |
| Recommendation interval | Anbefalingsinterval         | Empfehlungsintervall       | Intervalo de recomendación       |
| Forecast                | Prognose                    | Prognose                   | Previsión                        |
| Discharge floor         | Afladningsgulv              | Entladeuntergrenze         | Límite mínimo de descarga        |
| Temporary override      | Midlertidig tilsidesættelse | Temporäre Übersteuerung    | Anulación temporal               |
| Battery / Batteries     | Batteri(er)                 | Batterie(n)                | Batería(s)                       |
| Inverter                | Inverter                    | Wechselrichter             | Inversor                         |
| End of discharge        | Afladningsstop              | Entladeschluss             | Fin de descarga                  |

Keep product/protocol names and abbreviations untranslated everywhere: `HSEM`,
`OCPP`, `MILP`, `TOU`, `Solcast`, `Huawei`, entity/field acronyms like `SoC`.

**Tone**: imperative/infinitive, matching the English source ("Select the
sensor…", "Enable this option…"). For German use informal _du_-form
imperatives (Home Assistant's own translation convention), not formal _Sie_.
For Spanish use informal _tú_-form imperatives, neutral international Spanish
— no regional slang, no _vosotros_/_vos_.

## Step 4 — Re-run the validator

```bash
python3 scripts/validate_translations.py
```

Must report 0 missing, 0 stale, 0 placeholder mismatches for every language
before the PR is opened. Then format:

```bash
npx --yes prettier@3.1.0 --write custom_components/hsem/translations/*.json
```

## Known limitation — read before assuming a JSON fix is enough

Not every `entity.sensor.*` key in the translation files actually drives the
displayed entity name. Entities defined in `custom_sensors/*.py` generally
hardcode their English display name via a `get_*_sensor_name()` helper in
`utils/sensornames/` and override the HA `name` property directly — this wins
over `_attr_translation_key`/HA's translation-driven naming even when both are
set on the same class. Only entities using the `EntityDescription` +
`translation_key` pattern with no custom `name` property override (switch,
number, select, time platforms, and the two `custom_selectors/`) are actually
localized through these JSON files today.

Keeping `entity.sensor.*` translations complete and accurate in this skill is
still correct — it keeps the files consistent and ready for the day that
sensor-naming architecture is unified — but don't assume adding a translation
here changes what a non-English user actually sees on a `custom_sensors/*.py`
entity. If a task is specifically about localizing those sensor names, that
requires a code change (removing the `name` property override in favor of
`_attr_translation_key` + `_attr_has_entity_name`), which is a larger,
separate change outside this skill's scope — flag it, don't silently attempt
it as part of a translation-only PR.

## Anti-Patterns to Avoid

- ❌ Adding a new config/options flow field in English only
- ❌ Copy-pasting the English value into `da`/`de`/`es.json` as a placeholder
  and forgetting to translate it later
- ❌ Translating `config.step.X` but not the duplicate `options.step.X`
- ❌ Leaving a stale key behind after renaming a field
- ❌ Inventing new terminology instead of reusing the glossary in Step 3
- ❌ Assuming a `entity.sensor.*` translation fixes what users actually see —
  check whether that entity's Python class overrides `name` (see "Known
  limitation" above)
