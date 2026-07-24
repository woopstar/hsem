#!/bin/sh
# Bridges SSH and GPG agents from macOS host to container over TCP.
#
# Docker Desktop for Mac runs inside a Linux VM and cannot forward
# Unix domain sockets. This script uses socat to relay the host-side
# SSH agent (gpg-agent.ssh), GPG agent (gpg-agent), and scdaemon
# sockets from the macOS host into the container over TCP.
#
# Sockets are created in /tmp (container-local) because Docker for Mac's
# bind-mounted filesystems (osxfs) do not support Unix sockets.
#
# GPG needs a writable homedir for lock files and stub generation.
# We copy the host's read-only gnupg data to a container-local directory.

set -e

SSH_PORT="${SSH_AGENT_PORT:-9999}"
GPG_PORT="${GPG_AGENT_PORT:-9998}"
SCDAEMON_PORT="${SCDAEMON_PORT:-9997}"
AGENT_HOST="${SSH_AGENT_HOST:-host.docker.internal}"

# Kill any stale relays
pkill -f "socat.*${SSH_PORT}" 2>/dev/null || true
pkill -f "socat.*${GPG_PORT}" 2>/dev/null || true
pkill -f "socat.*${SCDAEMON_PORT}" 2>/dev/null || true

# Create TCP→Unix relays in /tmp
socat UNIX-LISTEN:/tmp/ssh-agent.sock,fork,mode=0600 \
    TCP:"${AGENT_HOST}:${SSH_PORT}" &

socat UNIX-LISTEN:/tmp/S.gpg-agent,fork,mode=0600 \
    TCP:"${AGENT_HOST}:${GPG_PORT}" &

socat UNIX-LISTEN:/tmp/S.scdaemon,fork,mode=0600 \
    TCP:"${AGENT_HOST}:${SCDAEMON_PORT}" &

# Wait for relays to be ready
for i in $(seq 1 10); do
    if [ -S /tmp/ssh-agent.sock ] && [ -S /tmp/S.gpg-agent ] && [ -S /tmp/S.scdaemon ]; then
        break
    fi
    sleep 0.5
done

echo "SSH agent bridge ready:  /tmp/ssh-agent.sock -> ${AGENT_HOST}:${SSH_PORT}"
echo "GPG agent bridge ready:   /tmp/S.gpg-agent -> ${AGENT_HOST}:${GPG_PORT}"
echo "scdaemon bridge ready:    /tmp/S.scdaemon -> ${AGENT_HOST}:${SCDAEMON_PORT}"

# Copy host gnupg data to container-local writable directory, then
# replace the sockets with our relayed ones
rm -rf /tmp/gpg-home
cp -a /root/.gnupg /tmp/gpg-home
rm -f /tmp/gpg-home/S.gpg-agent /tmp/gpg-home/S.scdaemon /tmp/gpg-home/S.gpg-agent.ssh
ln -sf /tmp/S.gpg-agent /tmp/gpg-home/S.gpg-agent
ln -sf /tmp/S.scdaemon /tmp/gpg-home/S.scdaemon
ln -sf /tmp/ssh-agent.sock /tmp/gpg-home/S.gpg-agent.ssh

export SSH_AUTH_SOCK=/tmp/ssh-agent.sock
export GNUPGHOME=/tmp/gpg-home

# Create writable global git config (host ~/.gitconfig is mounted read-only
# and may point to /opt/homebrew/bin/gpg which doesn't exist in the container)
cp /root/.gitconfig /tmp/gitconfig 2>/dev/null || true
git config -f /tmp/gitconfig gpg.program /usr/bin/gpg 2>/dev/null || true
export GIT_CONFIG_GLOBAL=/tmp/gitconfig

echo "GPG homedir ready: /tmp/gpg-home"
echo "Environment: SSH_AUTH_SOCK=$SSH_AUTH_SOCK GNUPGHOME=$GNUPGHOME"

# Keep the relay alive
exec "$@"