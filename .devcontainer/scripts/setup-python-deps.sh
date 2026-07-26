#!/bin/sh
# Installs all Python dependencies for the HSEM devcontainer.
# Runs as postCreateCommand — always, whether local dev or CI.
set -eu

WORKSPACE_DIR="${WORKSPACE_DIR:-/workspaces/hsem}"

pip install --no-cache-dir \
    --requirement "${WORKSPACE_DIR}/requirements.txt" \
    --requirement "${WORKSPACE_DIR}/requirements_test.txt" \
    --requirement "${WORKSPACE_DIR}/requirements_lint.txt" \
    --requirement "${WORKSPACE_DIR}/requirements_typing.txt"