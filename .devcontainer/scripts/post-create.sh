#!/bin/bash
# postCreateCommand wrapper for the HSEM devcontainer.
#
# Installs Python dependencies (always) and runs a one-shot smoke test of
# the SSH/GPG agent bridge (local dev only).
#
# Bridge failure is fatal here: without the bridge, git signing and
# pushes silently break, so failing fast at create time surfaces the
# problem immediately instead of mid-workflow. CI skips the bridge.

set -eu

/usr/local/bin/setup-python-deps.sh

if [ -z "${CI:-}" ]; then
    /usr/local/bin/devcontainer-agent-bridge.sh \
        /bin/sh -c 'echo SSH agent ready in container'
fi
