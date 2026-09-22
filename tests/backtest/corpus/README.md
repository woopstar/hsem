# Backtest corpus

Committed `hsem.export_diagnostics` dumps, replayed offline by
`tests/backtest/`. See `docs/backtest-harness.md` for the workflow.

## Why these are committed

The suite's other planner tests run on synthetic fixtures, which by
construction cannot contain the input combinations that actually reach users —
issue #1032 shipped a contradiction to v6.3.2 while every fixture-based test
passed. These dumps make the spec invariants run against real inputs, and they
keep doing so in CI without a live Home Assistant.

## Provenance and redaction

| File                           | Source cycle     | Notes                                                        |
| ------------------------------ | ---------------- | ------------------------------------------------------------ |
| `cycle-2026-09-14-1721.json`   | 2026-09-14 17:21 | 15-min slots, 48 h horizon, EV disabled, battery at 100 % SoC |

`build_diagnostics_dump` redacts HA entity IDs, tokens and passwords before a
dump is returned — that is what makes these safe to attach to GitHub issues in
the first place. The committed files were additionally checked to contain no
entity IDs, URLs, credentials or personal identifiers. What remains is public
day-ahead spot prices, a household load/PV profile, and battery hardware
settings.

**Do not commit a dump you have not read.** A dump is a snapshot of someone's
home.

## Regenerating

`PlannerInput` fields get added and removed. When that happens a corpus entry
stops round-tripping losslessly and `test_corpus_replay.py` fails on
`ReplayReport.is_faithful` — that failure is the point, not a nuisance: it says
the recorded cycle no longer describes an input the current planner accepts.

Refresh an entry by replaying it through the current code and re-emitting it:

```bash
python3 scripts/replay_planner_input.py \
    tests/backtest/corpus/cycle-2026-09-14-1721.json \
    --regenerate tests/backtest/corpus/cycle-2026-09-14-1721.json
```

The regenerated file records the replaying checkout's `hsem_version` and keeps
the source cycle's `dump_timestamp`, so regenerating an unchanged entry
produces an unchanged file. Review the diff: a field that silently disappeared
is exactly the kind of drift this harness exists to catch.
