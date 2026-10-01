#!/usr/bin/env bash
#
# One command to run the planner backtest and grow the committed corpus
# (issue #1037).  See docs/backtest-runbook.md.
#
#   1. copy the live corpus off Home Assistant (scp; needs HA_SSH_HOST)
#   2. collect realized actuals for recent days      (scripts/collect_actuals.sh)
#   3. replay every new cycle against the planner spec, and commit the ones
#      that cover a new situation, plus actuals for the days they cover
#                                                     (scripts/backtest_harvest.py)
#   4. run the backtest suite over the committed corpus
#
# It never commits to git: review the new files and commit them yourself.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
ENV_FILE="${HSEM_ENV_FILE:-${REPO_ROOT}/.env}"
# _env.sh is shellchecked on its own; CI does not run with -x to follow it.
# shellcheck disable=SC1091
source "${REPO_ROOT}/scripts/_env.sh"
load_env_file "${ENV_FILE}"

DATA_DIR="${HOME}/hsem-actuals"
LIVE_DIR="${HSEM_BACKTEST_CORPUS:-${DATA_DIR}/corpus}"
# .env values are taken literally, so expand a leading ~ here.
LIVE_DIR="${LIVE_DIR/#\~/${HOME}}"
DAYS=8
SKIP_FETCH=0
DRY_RUN=0
HARVEST_ARGS=()

usage() {
    cat <<EOF
Usage: $(basename "$0") [options]

Options:
  --days N       Days of actuals to fetch, counting back from yesterday (default: ${DAYS})
  --max-new N    Cycles to add to the committed corpus in this run
  --max-corpus N Committed cycles in total
  --all          Replay the whole live corpus, not just cycles since the last run
  --skip-fetch   Use what is already on disk: no scp, no Home Assistant calls
  --dry-run      Report what would be added; write nothing to tests/backtest
  -h, --help     This message

Settings come from ${ENV_FILE} (see .env.example):
  HA_URL, HA_TOKEN, TZ        for collect_actuals.sh
  HA_SSH_HOST                 Home Assistant host to scp the corpus from
  HA_SSH_PORT, HA_SSH_USER    default 22 and root
  HA_CORPUS_PATH              default /config/hsem-corpus.jsonl
  HSEM_BACKTEST_CORPUS        live corpus directory (default ${DATA_DIR}/corpus)
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --days) DAYS="$2"; shift 2 ;;
        --max-new|--max-corpus) HARVEST_ARGS+=("$1" "$2"); shift 2 ;;
        --all) HARVEST_ARGS+=(--all); shift ;;
        --skip-fetch) SKIP_FETCH=1; shift ;;
        --dry-run) DRY_RUN=1; HARVEST_ARGS+=(--dry-run); shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "[error] unknown option: $1" >&2; usage; exit 2 ;;
    esac
done

step() { echo ""; echo "=== $* ==="; }

# 1. Live corpus -------------------------------------------------------------
step "1/4 live corpus"
mkdir -p "${LIVE_DIR}"
if [[ "${SKIP_FETCH}" -eq 1 ]]; then
    echo "[info] --skip-fetch: using ${LIVE_DIR} as is"
elif [[ -z "${HA_SSH_HOST:-}" ]]; then
    echo "[info] HA_SSH_HOST not set: not copying; using ${LIVE_DIR} as is"
else
    target="${LIVE_DIR}/hsem-corpus.jsonl"
    source_path="${HA_SSH_USER:-root}@${HA_SSH_HOST}:${HA_CORPUS_PATH:-/config/hsem-corpus.jsonl}"
    # Copy to a temporary name first: an interrupted copy must not replace a
    # good corpus with a truncated one.
    scp -q -P "${HA_SSH_PORT:-22}" "${source_path}" "${target}.part"
    mv "${target}.part" "${target}"
    echo "[ok]   $(grep -c '^{' "${target}") cycles in ${target}"
fi

# 2. Actuals -----------------------------------------------------------------
step "2/4 actuals"
actuals_args=(--days "${DAYS}" --out "${DATA_DIR}")
[[ "${SKIP_FETCH}" -eq 1 ]] && actuals_args+=(--skip-fetch)
if [[ "${SKIP_FETCH}" -eq 1 && ! -d "${DATA_DIR}/raw" ]]; then
    echo "[info] no raw history in ${DATA_DIR}/raw yet; skipping"
else
    "${REPO_ROOT}/scripts/collect_actuals.sh" "${actuals_args[@]}"
fi

# 3. Backtest + harvest ------------------------------------------------------
step "3/4 backtest new cycles and harvest"
harvest_status=0
"${PYTHON}" "${REPO_ROOT}/scripts/backtest_harvest.py" \
    --live "${LIVE_DIR}" \
    --actuals "${DATA_DIR}/actuals.json" \
    --quarantine "${DATA_DIR}/quarantine" \
    "${HARVEST_ARGS[@]}" || harvest_status=$?

# 4. Committed suite ---------------------------------------------------------
step "4/4 backtest suite over the committed corpus"
suite_status=0
(cd "${REPO_ROOT}" && "${PYTHON}" -m pytest tests/backtest/ -q --no-cov \
    -p no:cacheprovider) || suite_status=$?

echo ""
echo "=== summary ==="
if [[ "${DRY_RUN}" -eq 0 ]]; then
    changes="$(cd "${REPO_ROOT}" && git status --short -- tests/backtest/corpus tests/backtest/actuals)"
    if [[ -n "${changes}" ]]; then
        echo "new files to review and commit:"
        echo "${changes}"
        echo ""
        echo "  git add tests/backtest/corpus tests/backtest/actuals"
        echo "  git commit -m \"test(backtest): add corpus cycles from <dates>\""
    else
        echo "no new files: every new cycle repeated a situation already covered"
    fi
fi
[[ "${harvest_status}" -ne 0 ]] && echo "[!] a new cycle violated a planner invariant -- see ${DATA_DIR}/quarantine"
[[ "${suite_status}" -ne 0 ]] && echo "[!] the backtest suite failed"
exit $(( harvest_status || suite_status ))
