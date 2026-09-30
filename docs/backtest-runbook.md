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

To keep one `.env` for several checkouts, put it anywhere and point at it:

```bash
export HSEM_ENV_FILE=~/hsem-actuals/.env
```

`TZ` must be Home Assistant's time zone, or every day boundary shifts.
`HSEM_ARCHIVE_ENTITIES` are downloaded but not converted — the recorder purges,
so anything you might want later has to be fetched now.

---

## Each run

### 1. Copy the corpus off Home Assistant

With the SSH add-on (use the port it listens on):

```bash
mkdir -p ~/hsem-actuals/corpus
scp -P <port> root@<ha-host>:/config/hsem-corpus.jsonl ~/hsem-actuals/corpus/
```

Copying while Home Assistant is still appending is fine: a half-written last
line is skipped.

### 2. Collect actuals for the same days

```bash
./scripts/collect_actuals.sh --days 8 --verify
```

This fetches each complete day not already in `~/hsem-actuals/raw/`, converts
them all, and aligns the result against the committed corpus cycle. Re-running
is cheap: days already downloaded are skipped. Add `--refresh` to download them
again — needed once after adding entities to the mapping or to
`HSEM_ARCHIVE_ENTITIES`.

Check two things in the output:

- **Every series has the same slot count** (96 per day at 15-minute slots). A
  series far below the others means that entity is not a cumulative kWh meter,
  or is not recorded.
- **The `prices:` verdict.** `same prices, slot for slot` is ideal. `plan carries
hourly prices while the export is sub-hourly` is expected against the
  committed cycle, which predates 15-minute prices. **`systematic offset`
  means stop**: a fee differs between what the planner used and what was
  recorded, and every cost comparison would carry it.

### 3. Replay the corpus

Quick check — the full backtest suite, first 25 cycles of each corpus file:

```bash
HSEM_BACKTEST_CORPUS=~/hsem-actuals/corpus python -m pytest tests/backtest/ -q
```

Every cycle:

```bash
python3 scripts/backtest_corpus.py ~/hsem-actuals/corpus
```

Example from a real 340-cycle corpus:

```text
cycles: 340  in 276s (812 ms/cycle)
versions: {'7.0.0-beta1': 340}
winners: {'milp': 340}
fidelity: every cycle round-trips losslessly
invariants: none violated
```

It exits non-zero when any cycle violates an invariant, and names the first
cycle for each one. Use `--limit N` for a quicker look. A real cycle takes
anywhere from a few hundred milliseconds to about a second, depending on the
machine.

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
the pytest corpus tests fail on the committed cycle. Both are deliberate.

- **Your live corpus**: nothing to do. New dumps carry the field once Home
  Assistant runs the new build; older ones replay with its default.
- **The committed cycle**: regenerate it, but first check what the new field
  defaults to. `--regenerate` fills it with the dataclass default, which must
  describe the recorded site. See `tests/backtest/corpus/README.md` for the
  command and for how `time_zone` (#1169) was handled.

---

## Troubleshooting collection

| Symptom                                                                     | Cause                                                                                                |
| --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `hsem-corpus.jsonl` never appears                                           | The automation targets `notify.send_message` with an entity. Call `notify.hsem_corpus` as a service. |
| Automation error `Type is not JSON serializable: numpy.float64`             | An HSEM build without the `export_diagnostics` serialisation fix (PR #1093). Update HSEM.            |
| First line of the corpus is `Home Assistant notifications (Log started: …)` | Normal — the file notifier's header. It is skipped.                                                  |
| `[error] <day> failed` from `collect_actuals.sh`                            | Wrong `HA_URL` or `HA_TOKEN`, or the recorder no longer holds that day.                              |
