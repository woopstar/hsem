#!/bin/sh
# Starts the SSH agent TCP relay on the macOS host.
# Run this before starting the devcontainer.
# Uses Python 3 (ships with macOS) - no external dependencies needed.

DIR="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$DIR/start-agent-relay.py" "$@"