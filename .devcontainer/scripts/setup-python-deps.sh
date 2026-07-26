#!/bin/sh
# Installs all Python dependencies for the HSEM devcontainer.
# Runs as postCreateCommand — always, whether local dev or CI.
set -eu

pip install --no-cache-dir \
    --requirement /workspaces/hsem/requirements.txt \
    --requirement /workspaces/hsem/requirements_test.txt \
    --requirement /workspaces/hsem/requirements_lint.txt \
    --requirement /workspaces/hsem/requirements_typing.txt