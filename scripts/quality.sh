#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.." || exit

# rtk is typically installed to ~/.local/bin, which may be missing from
# non-interactive shells (CI, hooks). Add it if present.
if [[ -d "$HOME/.local/bin" ]]; then
    PATH="$HOME/.local/bin:$PATH"
fi

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

usage() {
    cat <<EOF
Usage: quality <command>

Commands:
  lint      Format and lint code (ruff format + ruff check)
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
            --timeout=120 \
            --cov=custom_components \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        ;;
    all)
        echo "=== Lint ==="
        run ruff format .
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
