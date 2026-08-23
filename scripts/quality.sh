#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.." || exit

# Run a command through rtk when available, otherwise run it directly.
run() {
    if command -v rtk >/dev/null 2>&1; then
        rtk "$@"
    else
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
