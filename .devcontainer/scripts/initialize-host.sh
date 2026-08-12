#!/bin/sh
# Stages host SSH config and gitconfig for devcontainer bind mounts.
#
# Devcontainer variable substitution cannot reliably resolve the correct host
# path on every platform (e.g. WSL2 reports a Linux HOME but the user's SSH
# keys live under their Windows user profile). This script runs on the host
# before the container starts, snapshots the relevant files into a known
# workspace-relative path, and ensures the mount sources always exist.
#
# Supported host platforms: native Linux, native macOS, Windows (native
# PowerShell calls the companion .ps1 script), and WSL2 on Windows.

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
STAGE_DIR="${REPO_ROOT}/.devcontainer/host-config"

mkdir -p "${STAGE_DIR}"

# Locate the host directory that contains .ssh and .gitconfig.
HOST_CONFIG_DIR=""

# WSL2: prefer the Windows user profile even though $HOME points to the
# WSL filesystem. The user's YubiKey/GnuPG setup is on the Windows side.
if [ -f /proc/sys/kernel/osrelease ] && grep -qi microsoft /proc/sys/kernel/osrelease; then
    WIN_USERPROFILE=""
    if command -v powershell.exe >/dev/null 2>&1; then
        WIN_USERPROFILE="$(powershell.exe -NoProfile -Command 'Write-Host -NoNewline $env:USERPROFILE' 2>/dev/null || true)"
    elif command -v cmd.exe >/dev/null 2>&1; then
        WIN_USERPROFILE="$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r' || true)"
    fi

    if [ -n "${WIN_USERPROFILE}" ] && command -v wslpath >/dev/null 2>&1; then
        HOST_CONFIG_DIR="$(wslpath -u "${WIN_USERPROFILE}")"
    fi
fi

# Fall back to the Unix home directory for native Linux/macOS or WSL setups
# without a resolvable Windows profile.
if [ -z "${HOST_CONFIG_DIR}" ]; then
    if [ -z "${HOME:-}" ]; then
        echo "Unable to determine host config directory (HOME not set)." >&2
        exit 1
    fi
    HOST_CONFIG_DIR="${HOME}"
fi

echo "Staging host config from: ${HOST_CONFIG_DIR}"

# .ssh: copy if it exists, otherwise create an empty directory so the bind
# mount source always exists and the container can start.
if [ -d "${HOST_CONFIG_DIR}/.ssh" ]; then
    rm -rf "${STAGE_DIR}/.ssh"
    cp -R "${HOST_CONFIG_DIR}/.ssh" "${STAGE_DIR}/.ssh"
else
    mkdir -p "${STAGE_DIR}/.ssh"
fi

# .gitconfig: copy if it exists, otherwise create an empty file.
if [ -f "${HOST_CONFIG_DIR}/.gitconfig" ]; then
    cp -f "${HOST_CONFIG_DIR}/.gitconfig" "${STAGE_DIR}/.gitconfig"
else
    : > "${STAGE_DIR}/.gitconfig"
fi

echo "Host config staged in: ${STAGE_DIR}"
