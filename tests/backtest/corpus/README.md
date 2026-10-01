# Backtest corpus

Committed `hsem.export_diagnostics` dumps, replayed offline by
`tests/backtest/`. See `docs/backtest-harness.md` for the workflow.

## Why these are committed

The suite's other planner tests run on synthetic fixtures, which by
construction cannot contain the input combinations that actually reach users —
issue #1032 shipped a contradiction to v6.3.2 while every fixture-based test
passed. These dumps make the spec invariants run against real inputs, and they
keep doing so in CI without a live Home Assistant.

## How it grows

`scripts/backtest_update.sh` (see `docs/backtest-runbook.md`) adds cycles from a
live corpus, but only those that cover a situation not already here — which
plan won, which modes it uses, the SoC band, and EV, negative-price and DST
flags. Harvested files keep only `planner_input`, the version, the timestamp
and `apply_result` (about 44 KB); `planner_output` is never read, since replays
recompute it. Each must round-trip losslessly and contain no entity id. The
corpus is capped at 50 cycles because every test run replays each one.

## Which installation a file is from

Every committed cycle and every file in `tests/backtest/actuals/` carries a
top-level `site` tag (issue #1225). The harness only compares a plan with
realized values when both carry the same tag, so one house's plan is never
scored against another house's meters.

| Tag      | Installation                                              | Files                                                           |
| -------- | --------------------------------------------------------- | --------------------------------------------------------------- |
| `site-a` | The collecting installation: 15 kWh battery, 35 A fuse    | the September 24 and 25 cycles, both actuals days               |
| `site-b` | The installation of a bug report: 10 kWh battery, 25 A fuse | `cycle-2026-09-14-1721.json`; no actuals were ever collected for it |

`actuals-2026-09-15.json` was committed because the September 14 cycle's
48-hour horizon covers that day. It is a `site-a` day: valid for scoring, but
not paired with that cycle.

The tag comes from `HSEM_BACKTEST_SITE` in `.env`. It is a label, not an
identity: lower-case letters, digits and hyphens, at most 32 characters, so no
name, address, host or entity id fits. A file without a tag still loads; two
untagged files pair with each other, an untagged file never pairs with a
tagged one, and nothing untagged is committed.

## Provenance and redaction

| File                           | Source cycle     | Notes                                                        |
| ------------------------------ | ---------------- | ------------------------------------------------------------ |
| `cycle-2026-09-14-1721.json`   | 2026-09-14 17:21 | 15-min slots, 48 h horizon, EV disabled, battery at 100 % SoC; the diagnostics download of a bug report (`site-b`) |

`build_diagnostics_dump` redacts HA entity IDs, tokens and passwords before a
dump is returned — that is what makes these safe to attach to GitHub issues in
the first place. The committed files were additionally checked to contain no
entity IDs, URLs, credentials or personal identifiers. What remains is public
day-ahead spot prices, a household load/PV profile, and battery hardware
settings.

**Do not commit a dump you have not read.** A dump is a snapshot of someone's
home.

## When `PlannerInput` changes

Adding or removing a `PlannerInput` field makes every committed cycle stop
round-tripping, and `test_corpus_replay.py` fails on `ReplayReport.is_faithful`.
That failure is the point: it says the recorded cycles no longer describe an
input the current planner accepts. The fix is one command:

```bash
python3 scripts/backtest_harvest.py --refresh-corpus
```

It fills each field the dumps predate with its `PlannerInput` default, drops
fields that no longer exist, replays every rewritten cycle against the spec,
and lists what it filled:

```text
0 cycle(s) already round-trip, 9 updated
  filled with the PlannerInput default — check each describes a site recorded before the field existed:
    battery_target_soc_enabled = False
    battery_target_soc_pct = 100.0
    battery_target_soc_time = '17:00:00'
    dynamic_floor_profile = None
```

**Read that list.** A default is right when it means "the feature did not exist
yet" — a target that is disabled, a profile that is absent. It is wrong when the
default selects different behaviour from what the site actually had. That was
the case for `time_zone` (#1169): its default `None` means the legacy
fixed-offset path, while a current dump carries the Home Assistant zone key, so
the committed cycle was given `time_zone="Europe/Copenhagen"` by hand. When in
doubt, set the real value in the file and re-run the refresh to verify.

Commit the refreshed files with the change that added the field. `--dry-run`
shows the list without writing.
