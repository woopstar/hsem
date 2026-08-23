#!/bin/bash
# Self-healing agent bridge check for the HSEM devcontainer.
#
# Sourced from /root/.bashrc on every interactive shell start. If the
# socat relay sockets are missing (host LaunchAgent died, container
# restarted without postStartCommand, etc.), restart the bridge in the
# background so SSH/GPG agent forwarding "just works".
#
# Silent when healthy; prints a banner only when it repaired or could
# not repair the bridge.

[ -z "${CI:-}" ] || return 0 2>/dev/null || exit 0
case "$-" in *i*) ;; *) return 0 2>/dev/null || exit 0 ;; esac

if [ -S /tmp/ssh-agent.sock ] && [ -S /tmp/S.gpg-agent ] && [ -S /tmp/S.scdaemon ]; then
    return 0 2>/dev/null || exit 0
fi

echo "hsem: agent bridge sockets missing — restarting..."
nohup /usr/local/bin/devcontainer-agent-bridge.sh /bin/true \
    >/tmp/agent-bridge.log 2>&1 &

# Give the relays a moment to bind their sockets.
i=0
while [ "$i" -lt 10 ]; do
    if [ -S /tmp/ssh-agent.sock ] && [ -S /tmp/S.gpg-agent ] && [ -S /tmp/S.scdaemon ]; then
        echo "hsem: agent bridge restarted OK (log: /tmp/agent-bridge.log)"
        return 0 2>/dev/null || exit 0
    fi
    sleep 0.5
    i=$((i + 1))
done

echo "hsem: WARNING agent bridge failed to start — SSH/GPG forwarding is DOWN."
echo "hsem:   check the host relay: sh .devcontainer/scripts/install-agent-relay.sh status"
echo "hsem:   container log:        /tmp/agent-bridge.log"
