#!/bin/bash
# postStartCommand wrapper for the HSEM devcontainer.
#
# Starts the SSH/GPG agent bridge in the background on every container
# start. Backgrounding is required: devcontainer waits for this command
# to finish, and the bridge keeps its socat relays alive in the
# foreground. Logs go to /tmp/agent-bridge.log.
#
# Best-effort by design: a failed bridge on restart should not block
# container startup; check /tmp/agent-bridge.log if agents stop working.

if [ -z "${CI:-}" ]; then
    nohup /usr/local/bin/devcontainer-agent-bridge.sh /bin/true \
        >/tmp/agent-bridge.log 2>&1 &
fi
