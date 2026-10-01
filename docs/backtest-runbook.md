# Backtest Runbook

Step-by-step instructions for collecting data from a live Home Assistant
install and replaying it through the planner. For _why_ the harness works the
way it does, see [Planner Backtest Harness](backtest-harness.md).

Commands run from the repository root unless stated otherwise.

---

## One-time setup

### 1. Keep enough recorder history

The actuals come from the recorder, and 15-minute detail is only kept for
`purge_keep_days` (default **10**). In `configuration.yaml`:

```yaml
recorder:
  purge_keep_days: 45 # or more — anything older is gone for good
```

### 2. Record a planner input every cycle

In `configuration.yaml`:

```yaml
notify:
  - platform: file
    name: hsem_corpus
    filename: hsem-corpus.jsonl
    timestamp: false
```

And an automation:

```yaml
automation:
  - alias: HSEM backtest corpus
    triggers:
      - trigger: time_pattern
        minutes: "/5" # match your HSEM update interval
    actions:
      - action: hsem.export_diagnostics
        response_variable: dump
      - action: notify.hsem_corpus
        data:
          message: "{{ dump | to_json }}"
```

Restart Home Assistant. Two things that look right but are not:

- Call **`notify.hsem_corpus` as a service**. The YAML `notify:` platform creates
  no entity, so `notify.send_message` with `target.entity_id` silently writes
  nothing.
- Trigger on a **time pattern**. The working-mode sensor only changes when the
  recommendation changes, so a state trigger skips the stable stretches.

Confirm it is writing — about 12 lines an hour at 5-minute cycles:

```bash
wc -l /config/hsem-corpus.jsonl
```

Budget about 120 KB per cycle, roughly 35 MB a day.

### 3. Store your token

```bash
cp .env.example .env
chmod 600 .env
```

Then edit `.env`. At minimum:

```bash
HA_URL=http://homeassistant.local:8123
HA_TOKEN=<long-lived access token>
TZ=Europe/Copenhagen
HSEM_ARCHIVE_ENTITIES=sensor.my_ev_charger_power,sensor.my_ev_soc,select.batteries_working_mode
```

Create the token under your HA profile → **Security** → **Long-lived access
tokens**. `.env` is gitignored; only `.env.example` is committed. The script
parses the file rather than sourcing it, so the token needs no escaping, and
anything you export in the shell overrides it. It warns if the file is readable
by other users.

To have the corpus copied off Home Assistant automatically, add the SSH
details too (the SSH add-on's port; key-based login avoids a password prompt):

```bash
HA_SSH_HOST=homeassistant.local
HA_SSH_PORT=22
HA_SSH_USER=root
HA_CORPUS_PATH=/config/hsem-corpus.jsonl
```

To keep one `.env` for several checkouts, put it anywhere and point at it:

```bash
export HSEM_ENV_FILE=~/hsem-actuals/.env
```

`TZ` must be Home Assistant's time zone, or every day boundary shifts.
`HSEM_ARCHIVE_ENTITIES` are downloaded but not converted — the recorder purges,
so anything you might want later has to be fetched now.

---

## Each run

```bash
./scripts/backtest_update.sh
```

That is the whole routine. It runs four steps and prints a summary:

1. **Copies the live corpus** off Home Assistant with `scp` (skipped when
   `HA_SSH_HOST` is empty — then copy `hsem-corpus.jsonl` into
   `~/hsem-actuals/corpus/` yourself).
2. **Collects actuals** for the last 8 days (`collect_actuals.sh`).
3. **Backtests every new cycle** against the planner spec and **harvests** the
   ones worth keeping into `tests/backtest/corpus/`, plus actuals for the days
   they cover into `tests/backtest/actuals/`.
4. **Runs the backtest suite** over the committed corpus.

It never commits to git. The summary lists new files; review and commit them:

```bash
git add tests/backtest/corpus tests/backtest/actuals
git commit -m "test(backtest): add corpus cycles from <dates>"
```

Run it as often as you like — weekly is plenty. Only cycles newer than the last
run are replayed (the resume point is `~/hsem-actuals/corpus/.harvested-until`),
so each run takes minutes, not hours.

| Option           | Use                                                        |
| ---------------- | ---------------------------------------------------------- |
| `--dry-run`      | Show what would be added; write nothing to the repository. |
| `--skip-fetch`   | Use what is on disk: no `scp`, no Home Assistant calls.    |
| `--all`          | Replay the whole live corpus, ignoring the resume point.   |
| `--days N`       | Days of actuals to fetch (default 8).                      |
| `--max-new N`    | New cycles per run (default 10).                           |
| `--max-corpus N` | Committed cycles in total (default 50).                    |

### What gets committed

**Only cycles that cover a new situation.** Most cycles repeat one already in
the corpus: 340 real cycles contained just 8 distinct situations. A situation
is which plan won, which operating modes it uses, the starting SoC band
(quartiles), and whether it involves EV charging, negative prices or a DST
change. So the corpus grows by breadth, and the rare cases — negative prices,
DST days, EV sessions — get added the first time they happen.

**Only what the harness reads.** A committed cycle keeps `planner_input`, the
version, timestamp and `apply_result` — about 44 KB instead of 135. It must
round-trip losslessly and contain no entity id. Dumps from before #1169 get the
site's `TZ` filled into `time_zone`.

**Actuals for the days those cycles cover**, one file per day, and only when
every energy series is complete. A day with a recorder gap is reported as
`incomplete in the export` and left out rather than committed short.

**Caps.** Each committed cycle is replayed by every test run, so the corpus is
capped at 50 cycles and 10 per run. When a cap stops a new situation, the
report says so; raise the cap deliberately.

### When a cycle violates an invariant

The run reports it, copies the cycle to `~/hsem-actuals/quarantine/`, and exits
non-zero. It is **not** committed — that would turn CI red before the bug is
fixed. Replay it with `scripts/replay_planner_input.py`, and attach it to an
issue.

Replays lift production's 2-second solver time limit. A real cycle can take
1.5 s to solve on an idle machine; on a busy one the same input returns a worse,
time-limited plan and fails an invariant for no reason in the planner's logic.
The backtest checks what the planner decides, not how fast the solver is.

### Doing the steps by hand

Each step is a script you can run alone:

```bash
./scripts/collect_actuals.sh --days 8 --verify           # actuals + price check
python3 scripts/backtest_harvest.py --dry-run             # backtest + harvest
python3 scripts/backtest_corpus.py ~/hsem-actuals/corpus  # replay everything
HSEM_BACKTEST_CORPUS=~/hsem-actuals/corpus python -m pytest tests/backtest/ -q
```

`backtest_corpus.py` replays every cycle regardless of the resume point and
names the first cycle for each violated invariant. `collect_actuals.sh
--verify` also cross-checks recorded prices: `systematic offset` means stop —
a fee differs between what the planner used and what was recorded.

---

## Reading the results

| Output                                      | Meaning                                                        | Action                                                                                                            |
| ------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| `invariants: none violated`                 | The planner held the spec on every real cycle.                 | None.                                                                                                             |
| `<invariant>: N`                            | A real input broke a spec rule.                                | Replay the named cycle with `scripts/replay_planner_input.py` and file an issue. This is what the harness is for. |
| `fidelity: … <field>: N cycle(s)`           | The dumps predate or outlive a `PlannerInput` field.           | Expected after a planner change. The replay still runs, with that field at its default — see below.               |
| `corrupt dump in the middle of the corpus`  | A line other than the last is not valid JSON.                  | Lost data; the line number is in the message.                                                                     |
| `HSEM_BACKTEST_CORPUS=… is not a directory` | The path is wrong or not created yet.                          | Create it and copy the corpus in.                                                                                 |
| `no price series` hint                      | The price sensors were added after these days were downloaded. | Re-run `collect_actuals.sh` with `--refresh`.                                                                     |

---

## When the planner changes

A new `PlannerInput` field makes older dumps report it under `fidelity`, and
the pytest corpus tests fail on the committed cycles. Both are deliberate.

- **The committed corpus**: run
  `python3 scripts/backtest_harvest.py --refresh-corpus` and commit the result
  with the change that added the field. It fills the new field with its default
  and lists what it filled — check that each default means "the feature did not
  exist yet". `tests/backtest/corpus/README.md` explains the one case so far
  where it did not (`time_zone`, #1169).
- **Your live corpus**: nothing to do. New dumps carry the field once Home
  Assistant runs the new build; older ones replay with its default.

---

## Troubleshooting collection

| Symptom                                                                     | Cause                                                                                                |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `hsem-corpus.jsonl` never appears                                           | The automation targets `notify.send_message` with an entity. Call `notify.hsem_corpus` as a service. |
| Automation error `Type is not JSON serializable: numpy.float64`             | An HSEM build without the `export_diagnostics` serialisation fix (PR #1093). Update HSEM.            |
| First line of the corpus is `Home Assistant notifications (Log started: …)` | Normal — the file notifier's header. It is skipped.                                                  |
| `[error] <day> failed` from `collect_actuals.sh`                            | Wrong `HA_URL` or `HA_TOKEN`, or the recorder no longer holds that day.                              |
