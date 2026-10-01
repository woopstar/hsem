# Recorded days

Fully recorded days for the regret attribution (`tests/backtest/attribution.py`),
read by `tests/backtest/test_recorded_day.py` and by
`scripts/backtest_attribute.py` when it is called without arguments. See
`docs/backtest-harness.md` → _Recorded days_.

## Why they are not in the corpus

The attribution replays a day slot by slot, so it needs a planner cycle for
(almost) every slot. `tests/backtest/corpus/` holds cycles chosen because each
covers a different situation, and every one is replayed on every test run. A
day of consecutive cycles is neither.

## What a day holds

| File                    | Content                                                              |
| ----------------------- | -------------------------------------------------------------------- |
| `cycles.jsonl`          | One slim cycle per slot: the first whose `now_iso` falls in the slot |
| `actuals-<date>.json`   | The day's realized values                                            |
| `actuals-<date+1>.json` | The following day's: every plan of the day reaches into it           |

Cycles keep `planner_input`, the version, the timestamp, `apply_result` and the
`site` tag, as corpus cycles do. Every file carries the same site tag.

## Provenance

| Day          | Site     | Notes                                                                                       |
| ------------ | -------- | ------------------------------------------------------------------------------------------- |
| `2026-09-29` | `site-a` | 96 cycles, 15-minute slots, 48 h horizon, 15 kWh battery, EV planning on; HSEM 7.0.0 builds |

Four `PlannerInput` fields did not exist when that day was recorded and carry
their defaults: `battery_target_soc_enabled = False`,
`battery_target_soc_pct = 100.0`, `battery_target_soc_time = '17:00:00'` and
`dynamic_floor_profile = None`. Each means "the feature did not exist yet".

The files were checked to contain no entity IDs, URLs, credentials or personal
identifiers. What remains is public day-ahead spot prices, a household load
and PV profile, and battery and EV charger settings.

## Adding a day

```bash
python3 scripts/backtest_harvest.py --day <date>
```

It refuses a day with a cycle in fewer than half of its slots, a cycle that
does not round-trip or contains an entity id, or incomplete actuals for the
day or the following day. It commits nothing.

**Do not commit a day you have not read.** It is two days of someone's home.

## When `PlannerInput` changes

`python3 scripts/backtest_harvest.py --refresh-corpus` refreshes these days
together with the corpus. When a planner change moves the attribution of the
committed day, update the pinned numbers in `test_recorded_day.py` in the same
PR.
