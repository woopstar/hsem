#!/usr/bin/env bash
#
# Export realized actuals from Home Assistant's recorder and convert them into
# an hsem-actuals-1 file for the planner backtest harness (issue #1037).
#
# Fetches one JSON file per complete day, then converts every raw file it finds
# in one pass so day boundaries are stitched.  Re-running is safe: a day that
# was already downloaded is not fetched again.
#
# See docs/backtest-harness.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"

DAYS=7
OUT_DIR="${HOME}/hsem-actuals"
SLOT_MINUTES=15
MAX_SILENCE_MINUTES=10
SKIP_FETCH=0
VERIFY=0
REFRESH=0

# Every mapped series.  Override any entity with the matching env var when your
# sensors are named differently -- the series names on the right are fixed.
MAPPING=(
    "${HSEM_GRID_IMPORT_ENTITY:-sensor.energy_import_ps}=grid_import"
    "${HSEM_GRID_EXPORT_ENTITY:-sensor.energy_export_ps}=grid_export"
    "${HSEM_PV_ENTITY:-sensor.energy_yield_1_ps}=pv_produced"
    "${HSEM_HOUSE_LOAD_ENTITY:-sensor.energy_house_load_yield_without_ev_ps}=house_load"
    "${HSEM_BATTERY_CHARGE_ENTITY:-sensor.energy_battery_charge_ps}=battery_charged"
    "${HSEM_BATTERY_DISCHARGE_ENTITY:-sensor.energy_battery_discharge_ps}=battery_discharged"
    "${HSEM_BATTERY_SOC_ENTITY:-sensor.batteries_state_of_capacity}=battery_soc_pct"
    # Realized prices.  The planner reads its prices straight from these
    # sensors, so their recorded state *is* the price a slot was settled at --
    # confirm that with --verify before relying on it.  If you set
    # hsem_export_fee_per_kwh, the effective export price is this minus the fee.
    "${HSEM_IMPORT_PRICE_ENTITY:-sensor.energi_data_service}=import_price"
    "${HSEM_EXPORT_PRICE_ENTITY:-sensor.energi_data_service_produktion}=export_price"
)

usage() {
    cat <<EOF
Usage: HA_URL=... HA_TOKEN=... $(basename "$0") [options]

Options:
  --days N          Complete days to fetch, counting back from yesterday (default: ${DAYS})
  --out DIR         Output directory (default: ${OUT_DIR})
  --slot-minutes N  Planner slot width (default: ${SLOT_MINUTES})
  --max-silence N   Minutes of silence across all entities that mean an outage (default: ${MAX_SILENCE_MINUTES})
  --refresh         Re-download days already present in raw/
  --skip-fetch      Convert what is already in raw/ without contacting HA
  --verify          Align against the committed corpus cycle and cross-check
                    realized prices against the plan that cycle was built from
  -h, --help        This message

Environment:
  HA_URL, HA_TOKEN            required unless --skip-fetch
  TZ                          must match your Home Assistant timezone
  HSEM_ARCHIVE_ENTITIES       extra comma-separated entities to download but not
                              convert -- EV charger power, EV SoC, phase meters.
                              The recorder purges; these cannot be fetched later.
  HSEM_*_ENTITY               override any mapped entity (see MAPPING in this file)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --days) DAYS="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        --slot-minutes) SLOT_MINUTES="$2"; shift 2 ;;
        --max-silence) MAX_SILENCE_MINUTES="$2"; shift 2 ;;
        --refresh) REFRESH=1; shift ;;
        --skip-fetch) SKIP_FETCH=1; shift ;;
        --verify) VERIFY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[error] unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

RAW_DIR="${OUT_DIR}/raw"
ACTUALS="${OUT_DIR}/actuals.json"
mkdir -p "${RAW_DIR}"

# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

if [[ "${SKIP_FETCH}" -eq 0 ]]; then
    : "${HA_URL:?set HA_URL, e.g. http://homeassistant.local:8123}"
    : "${HA_TOKEN:?set HA_TOKEN to a long-lived access token}"

    entities=""
    for pair in "${MAPPING[@]}"; do
        entities+="${pair%%=*},"
    done
    entities+="${HSEM_ARCHIVE_ENTITIES:-}"
    entities="${entities%,}"

    echo "[info] timezone: ${TZ:-$(date +%Z)} -- must match Home Assistant's"
    fetched=0
    skipped=0
    for ((d = DAYS; d >= 1; d--)); do
        day="$(date -d "${d} days ago" +%Y-%m-%d)"
        target="${RAW_DIR}/history-${day}.json"
        if [[ -s "${target}" && "${REFRESH}" -eq 0 ]]; then
            skipped=$((skipped + 1))
            continue
        fi
        start="$(date -d "${d} days ago 00:00" +%Y-%m-%dT%H:%M:%S%:z)"
        end="$(date -d "$((d - 1)) days ago 00:00" +%Y-%m-%dT%H:%M:%S%:z)"
        # A partial file would look like a recorder outage, so write via a
        # temporary and only publish it once curl succeeded.
        if curl -sfG \
            -H "Authorization: Bearer ${HA_TOKEN}" \
            --data-urlencode "end_time=${end}" \
            --data-urlencode "filter_entity_id=${entities}" \
            --data minimal_response --data no_attributes \
            "${HA_URL}/api/history/period/${start}" > "${target}.part"; then
            mv "${target}.part" "${target}"
            fetched=$((fetched + 1))
            echo "[ok]   ${day}  $(du -h "${target}" | cut -f1)"
        else
            rm -f "${target}.part"
            echo "[error] ${day} failed -- check HA_URL, HA_TOKEN and that the recorder still holds this day" >&2
            exit 1
        fi
    done
    echo "[info] fetched ${fetched} day(s), ${skipped} already present"
fi

shopt -s nullglob
raw_files=("${RAW_DIR}"/history-*.json)
shopt -u nullglob
if [[ ${#raw_files[@]} -eq 0 ]]; then
    echo "[error] no history files in ${RAW_DIR}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Convert
# ---------------------------------------------------------------------------

map_args=()
for pair in "${MAPPING[@]}"; do
    map_args+=(--map "${pair}")
done

echo "[info] converting ${#raw_files[@]} day(s)"
"${PYTHON}" "${REPO_ROOT}/scripts/build_actuals.py" "${raw_files[@]}" \
    "${map_args[@]}" \
    --slot-minutes "${SLOT_MINUTES}" \
    --max-silence-minutes "${MAX_SILENCE_MINUTES}" \
    --now "$(date -d 'today 00:00' +%Y-%m-%dT%H:%M:%S%:z)" \
    --out "${ACTUALS}"

if ! "${PYTHON}" - "${ACTUALS}" <<'PRICECHECK'
import json
import sys

payload = json.load(open(sys.argv[1]))
sys.exit(0 if {"import_price", "export_price"} <= set(payload.get("slot_values", {})) else 1)
PRICECHECK
then
    echo "[hint] no price series -- the price sensors joined the mapping after" >&2
    echo "[hint] these days were downloaded. Re-run with --refresh." >&2
fi

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

if [[ "${VERIFY}" -eq 1 ]]; then
    echo ""
    echo "[info] aligning against the committed corpus cycle"
    ACTUALS_PATH="${ACTUALS}" "${PYTHON}" - <<'PY'
import os
import sys

sys.path.insert(0, os.getcwd())
from custom_components.hsem.planner.engine_core import run_planner
from tests.backtest.actuals import align_to_slots, load_actuals
from tests.backtest.replay import load_planner_input

inp, _ = load_planner_input("tests/backtest/corpus/cycle-2026-09-14-1721.json")
slots = run_planner(inp).slots
rows, report = align_to_slots(load_actuals(os.environ["ACTUALS_PATH"]), slots)
print(report.describe())

# The planner reads its prices from the same sensors this export records, so on
# overlapping slots the two must agree exactly.  A mismatch means the recorded
# state is not the price the plan was settled at -- a fee, the wrong entity, or
# a revision -- and scoring would book that difference as regret.
pairs = [
    (row.import_price, slot.price.import_price, row.export_price, slot.price.export_price)
    for row, slot in zip(rows, slots, strict=False)
    if row.import_price is not None and row.export_price is not None
]
if not pairs:
    print("\nprices: no overlapping slots carry prices -- re-run with --refresh")
else:
    worst = max(max(abs(a - b), abs(c - d)) for a, b, c, d in pairs)
    bad = sum(1 for a, b, c, d in pairs if abs(a - b) > 1e-6 or abs(c - d) > 1e-6)
    print(f"\nprices: {len(pairs)} slot(s) compared against the plan, "
          f"{bad} mismatched, worst delta {worst:.6f}")
PY
fi

echo ""
echo "[done] ${ACTUALS}"
