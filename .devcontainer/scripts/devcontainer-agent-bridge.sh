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

# Kill any stale relays and remove leftover socket files so the
# UNIX-LISTEN binds below don't fail on "address already in use".
pkill -f "socat.*${SSH_PORT}" 2>/dev/null || true
pkill -f "socat.*${GPG_PORT}" 2>/dev/null || true
pkill -f "socat.*${SCDAEMON_PORT}" 2>/dev/null || true
rm -f /tmp/ssh-agent.sock /tmp/S.gpg-agent /tmp/S.scdaemon

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
    echo "Waiting for relays to be ready... ($i/10)"
done

echo "SSH agent bridge ready:  /tmp/ssh-agent.sock -> ${AGENT_HOST}:${SSH_PORT}"
echo "GPG agent bridge ready:   /tmp/S.gpg-agent -> ${AGENT_HOST}:${GPG_PORT}"
echo "scdaemon bridge ready:    /tmp/S.scdaemon -> ${AGENT_HOST}:${SCDAEMON_PORT}"

# Copy host gnupg data to a container-local writable directory, then
# replace the sockets with our relayed ones.
# Skip host agent sockets here: they cannot be reliably copied across
# Docker Desktop bind mounts and are recreated as symlinks below.
#
# Source selection:
#  - macOS/Linux: the host ~/.gnupg bind mount at /root/.gnupg, or the
#    macOS/Linux installer snapshot under .devcontainer/gnupg-host.
#  - Windows: GnuPG lives in %APPDATA%\gnupg, so the Windows installer
#    snapshots the public key material into .devcontainer/gnupg-host
#    (inside the workspace bind mount) instead.
#
# CI creates an empty ~/.gitconfig directory as a bind-mount stub, so we
# must not copy it (git rejects a directory as its config file).
GPG_SRC=/root/.gnupg
GPG_SNAPSHOT=/workspaces/hsem/.devcontainer/gnupg-host
if [ ! -e "${GPG_SRC}/pubring.kbx" ] && [ ! -e "${GPG_SRC}/pubring.gpg" ] \
    && [ -d "${GPG_SNAPSHOT}" ]; then
    GPG_SRC="${GPG_SNAPSHOT}"
    echo "Using Windows gnupg snapshot: ${GPG_SNAPSHOT}"
fi
rm -rf /tmp/gpg-home
mkdir -p /tmp/gpg-home
if [ -d "${GPG_SRC}" ]; then
    find "${GPG_SRC}" -mindepth 1 -maxdepth 1 ! -type s -exec cp -a {} /tmp/gpg-home/ \;
fi
rm -f /tmp/gpg-home/S.gpg-agent /tmp/gpg-home/S.scdaemon /tmp/gpg-home/S.gpg-agent.ssh
ln -sf /tmp/S.gpg-agent /tmp/gpg-home/S.gpg-agent
ln -sf /tmp/S.scdaemon /tmp/gpg-home/S.scdaemon
ln -sf /tmp/ssh-agent.sock /tmp/gpg-home/S.gpg-agent.ssh

export SSH_AUTH_SOCK=/tmp/ssh-agent.sock
export GNUPGHOME=/tmp/gpg-home

# Create writable global git config (host ~/.gitconfig is mounted read-only
# and may point to /opt/homebrew/bin/gpg which doesn't exist in the
# container). In CI the stub is an empty directory; start from scratch then.
if [ -f /root/.gitconfig ]; then
    cp /root/.gitconfig /tmp/gitconfig
else
    : > /tmp/gitconfig
fi
git config -f /tmp/gitconfig gpg.program /usr/bin/gpg 2>/dev/null || true
export GIT_CONFIG_GLOBAL=/tmp/gitconfig

echo "GPG homedir ready: /tmp/gpg-home"
echo "Environment: SSH_AUTH_SOCK=$SSH_AUTH_SOCK GNUPGHOME=$GNUPGHOME"

# Keep the relay alive
exec "$@"
