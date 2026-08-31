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

# Format markdown/YAML/JSON with prettier, pinned to the same version as the
# pre-commit hook and the CI check so all three agree. Skipped with a warning
# when node isn't present rather than failing the whole gate — CI still
# enforces it, so a missing local toolchain can't let drift through.
PRETTIER_VERSION="3.1.0"
prettier_format() {
    if ! command -v npx >/dev/null 2>&1; then
        echo "[warn] npx not found — skipping prettier (CI will still check it)" >&2
        return 0
    fi
    run npx --yes "prettier@${PRETTIER_VERSION}" --write .
}

usage() {
    cat <<EOF
Usage: quality <command>

Commands:
  lint      Format and lint code (ruff format + ruff check + prettier)
  typing    Type check with mypy
  quality   Static quality checks (pyright + vulture)
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
        prettier_format
        ;;
    typing)
        run mypy custom_components tests
        ;;
    quality)
        run python -m pyright
        run python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
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
        prettier_format
        echo ""
        echo "=== Type Check ==="
        run mypy custom_components tests
        echo ""
        echo "=== Quality ==="
        run python -m pyright
        run python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
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
