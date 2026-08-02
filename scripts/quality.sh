#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.." || exit

# Pre-import NumPy/SciPy C extensions before pytest-cov's sysmon tracer starts.
# This works around a Python 3.14 + coverage.py incompatibility that otherwise
# causes scipy.optimize to fail with "cannot load module more than once per
# process" and makes the MILP/ML test suites skip.
export PYTHONPATH="${PWD}/scripts/sitecustomize_import${PYTHONPATH:+:${PYTHONPATH}}"

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
        ruff format .
        ruff check . --fix
        ;;
    typing)
        mypy custom_components tests
        ;;
    quality)
        python -m pyright
        python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
        ;;
    test)
        python -m pytest tests/ \
            --timeout=120 \
            --cov=custom_components.hsem \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        ;;
    all)
        echo "=== Lint ==="
        ruff format .
        echo ""
        echo "=== Type Check ==="
        mypy custom_components tests
        echo ""
        echo "=== Quality ==="
        python -m pyright
        python -m vulture custom_components/hsem tests vulture_whitelist.py --min-confidence 80
        echo ""
        echo "=== Tests ==="
        python -m pytest tests/ \
            --timeout=120 \
            --cov=custom_components.hsem \
            --cov-report=xml \
            --junitxml=test-results.xml \
            "${@:2}"
        ;;
    *)
        usage
        ;;
esac
