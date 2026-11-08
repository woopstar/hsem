#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.." || exit

# rtk is typically installed to ~/.local/bin, which may be missing from
# non-interactive shells (CI, hooks). Add it if present.
if [[ -d "$HOME/.local/bin" ]]; then
    PATH="$HOME/.local/bin:$PATH"
fi

# mypy/ruff/pytest caches aren't safe to share across concurrent worktrees
# (mypy in particular can race/corrupt on simultaneous writes), so namespace
# them per worktree under the shared cache root. uv's cache and
# PYTHONPYCACHEPREFIX are left container-wide: uv's is a content-addressed
# package cache meant to be shared, and pyc caches already mirror the full
# source path so different worktrees can't collide.
_worktree_id="$(pwd -P | md5sum | cut -c1-10)"
export MYPY_CACHE_DIR="${MYPY_CACHE_DIR:-/tmp/hsem-cache/mypy}/${_worktree_id}"
export RUFF_CACHE_DIR="${RUFF_CACHE_DIR:-/tmp/hsem-cache/ruff}/${_worktree_id}"
PYTEST_CACHE_DIR="${PYTEST_CACHE_DIR:-.pytest_cache}/${_worktree_id}"

# Run a command through rtk when available, otherwise run it directly.
# Set QUALITY_DEBUG=1 to print which mode is used for each command.
run() {
    if command -v rtk >/dev/null 2>&1; then
        if [[ "${QUALITY_DEBUG:-0}" == "1" ]]; then
            echo "[debug] rtk $*" >&2
        fi
        rtk "$@"
    else
        if [[ "${QUALITY_DEBUG:-0}" == "1" ]]; then
            echo "[debug] (no rtk) $*" >&2
        fi
        "$@"
    fi
}

# Prettier formats markdown/YAML/JSON.  This is the single source of truth for
# the version and the invocation: the pre-commit hook and the CI workflow both
# reach prettier through this script, so there is nothing to keep in sync.
# Scope is owned by .prettierignore, not by per-caller include/exclude lists.
PRETTIER_VERSION="3.1.0"

# Usage: prettier_run --write | --check
# --write is used by the fixing targets (lint/all), matching ruff's behaviour.
# --check is used by CI, which must never mutate the tree.
prettier_run() {
    local mode="$1"
    if ! command -v npx >/dev/null 2>&1; then
        if [[ "${mode}" == "--check" ]]; then
            echo "[error] npx not found — cannot verify prettier formatting" >&2
            return 1
        fi
        echo "[warn] npx not found — skipping prettier (CI will still check it)" >&2
        return 0
    fi
    run npx --yes "prettier@${PRETTIER_VERSION}" "${mode}" .
}

# The math in docs/ is rewritten silently by prettier (single-line $$ blocks)
# and by GitHub's Markdown escapes; see the rules in the script.  It only
# verifies, so the fixing targets run it after prettier to report what
# prettier just broke.
docs_math_check() {
    run python3 scripts/check_docs_math.py
}

# The 30 KiB / 1000-line rule of CODE_QUALITY_STANDARDS.md (File Size Limits).
# Nothing can fix an oversized file automatically, so this only verifies.
file_size_check() {
    run python3 scripts/check_file_size.py
}

# State that is created empty, read, and never filled (issue #1398): the
# sessions sensor showed 0 for that reason and coverage could not see it.
# Static, so it only verifies.
unfilled_state_check() {
    run python3 scripts/check_unfilled_state.py
}

# 100 % coverage on the lines a change adds or edits (issue #1386). The
# per-module floor above only protects the total, so a well-covered module
# could take new untested lines and still pass. Compared against
# PATCH_COVERAGE_BASE (default origin/main) using the coverage.xml the test
# run wrote. Lines that cannot be reached through the public interface may
# carry "# pragma: no cover"; nothing else is exempt.
#
# safe.directory is passed through the environment because CI runs this in
# the devcontainer, where the bind-mounted checkout belongs to another uid
# and git would otherwise refuse to read it.
PATCH_COVERAGE_BASE="${PATCH_COVERAGE_BASE:-origin/main}"

patch_coverage_check() {
    if [[ ! -f coverage.xml ]]; then
        echo "[error] coverage.xml not found — run './scripts/quality.sh test' first" >&2
        return 1
    fi
    (
        export GIT_CONFIG_COUNT=1
        export GIT_CONFIG_KEY_0=safe.directory
        GIT_CONFIG_VALUE_0="$(pwd -P)"
        export GIT_CONFIG_VALUE_0
        if ! git rev-parse --verify --quiet "${PATCH_COVERAGE_BASE}^{commit}" >/dev/null; then
            echo "[error] compare branch '${PATCH_COVERAGE_BASE}' not found — run 'git fetch origin main' (CI checks out with fetch-depth: 0)" >&2
            exit 1
        fi
        run diff-cover coverage.xml \
            --compare-branch="${PATCH_COVERAGE_BASE}" \
            --fail-under=100
    )
}

usage() {
    cat <<EOF
Usage: quality <command>

Commands:
  lint      Format and lint code (ruff format + ruff check + prettier --write + docs math check)
  typing    Type check with mypy
  quality   Static quality checks (pyright + vulture)
  format-check  Verify ruff format, ruff lint, prettier, docs math, file sizes and unfilled state without writing (used by CI)
  docs-math     Check that the LaTeX math in docs/ survives prettier and GitHub
  file-size     Assert every Python file is within the file-size limits
  unfilled-state  Assert no attribute is created empty, read, and never filled
  translations  Validate en/da/de/es translation files stay in sync
  skylos    Run Skylos static analysis (experimental, not in 'all')
  test      Run tests with pytest and coverage
  coverage-floor  Assert every module meets the 95% per-module coverage floor
  patch-coverage  Assert 100% coverage on lines changed vs \$PATCH_COVERAGE_BASE (default origin/main)
  all       Run lint, file-size, unfilled-state, typing, quality, translations, and test in sequence
EOF
    exit 1
}

case "${1:-}" in
    lint)
        run ruff format .
        run ruff check . --fix
        prettier_run --write
        docs_math_check
        ;;
    typing)
        run mypy custom_components tests
        ;;
    quality)
        run python -m pyright
        run python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
        echo ""
        echo "[info] Supplementary vulture pass (custom_components/hsem only, tests"
        echo "[info] excluded, --min-confidence 0). Informational only -- same-named"
        echo "[info] methods across classes still won't be flagged (issue #890)."
        run python -m vulture custom_components/hsem vulture_whitelist.py --min-confidence 0 || true
        ;;
    format-check)
        run ruff format --check .
        run ruff check .
        prettier_run --check
        docs_math_check
        file_size_check
        unfilled_state_check
        ;;
    docs-math)
        docs_math_check
        ;;
    file-size)
        file_size_check
        ;;
    unfilled-state)
        unfilled_state_check
        ;;
    translations)
        run python3 scripts/validate_translations.py
        ;;
    skylos)
        SKYLOS_GREP_BUDGET=120 run skylos custom_components -a
        ;;
    coverage-floor)
        run python3 scripts/check_coverage_floor.py
        ;;
    patch-coverage)
        patch_coverage_check
        ;;
    test)
        run python -m pytest tests/ \
            -o cache_dir="${PYTEST_CACHE_DIR}" \
            --timeout=120 \
            --cov=custom_components \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        # A subset run (extra pytest args) writes a partial report, so the
        # per-module floor is only meaningful for a full run.
        if [[ $# -le 1 ]]; then
            run python3 scripts/check_coverage_floor.py
            patch_coverage_check
        fi
        ;;
    all)
        echo "=== Lint ==="
        run ruff format .
        run ruff check . --fix
        prettier_run --write
        docs_math_check
        echo ""
        echo "=== File Size ==="
        file_size_check
        echo ""
        echo "=== Unfilled State ==="
        unfilled_state_check
        echo ""
        echo "=== Type Check ==="
        run mypy custom_components tests
        echo ""
        echo "=== Quality ==="
        run python -m pyright
        run python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
        echo ""
        echo "[info] Supplementary vulture pass (custom_components/hsem only, tests"
        echo "[info] excluded, --min-confidence 0). Informational only -- same-named"
        echo "[info] methods across classes still won't be flagged (issue #890)."
        run python -m vulture custom_components/hsem vulture_whitelist.py --min-confidence 0 || true
        echo ""
        echo "=== Translations ==="
        run python3 scripts/validate_translations.py
        echo ""
        echo "=== Tests ==="
        run python -m pytest tests/ \
            -o cache_dir="${PYTEST_CACHE_DIR}" \
            --timeout=120 \
            --cov=custom_components \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        # A subset run (extra pytest args) writes a partial report, so the
        # per-module floor is only meaningful for a full run.
        if [[ $# -le 1 ]]; then
            run python3 scripts/check_coverage_floor.py
            patch_coverage_check
        fi
        ;;
    *)
        usage
        ;;
esac
