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

usage() {
    cat <<EOF
Usage: quality <command>

Commands:
  lint      Format and lint code (ruff format + ruff check + prettier --write)
  typing    Type check with mypy
  quality   Static quality checks (pyright + vulture)
  format-check  Verify prettier formatting without writing (used by CI)
  skylos    Run Skylos static analysis (experimental, not in 'all')
  test      Run tests with pytest and coverage
  all       Run lint, typing, quality, and test in sequence
EOF
    exit 1
}

case "${1:-}" in
    lint)
        run ruff format .
        run ruff check . --fix
        prettier_run --write
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
        prettier_run --check
        ;;
    skylos)
        SKYLOS_GREP_BUDGET=120 run skylos custom_components -a
        ;;
    test)
        run python -m pytest tests/ \
            -o cache_dir="${PYTEST_CACHE_DIR}" \
            --timeout=120 \
            --cov=custom_components \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        ;;
    all)
        echo "=== Lint ==="
        run ruff format .
        prettier_run --write
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
        echo "=== Tests ==="
        run python -m pytest tests/ \
            -o cache_dir="${PYTEST_CACHE_DIR}" \
            --timeout=120 \
            --cov=custom_components \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        ;;
    *)
        usage
        ;;
esac
