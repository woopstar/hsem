#!/bin/sh
# Install/uninstall the HSEM agent relay as a macOS LaunchAgent.
#
# The relay bridges YubiKey GPG/SSH agent sockets over TCP so Docker
# containers can access them. This installs it as a per-user service
# that starts at login and restarts if it crashes.
#
# Usage:
#   sh install-agent-relay.sh install    # Install and start
#   sh install-agent-relay.sh uninstall  # Stop and remove
#   sh install-agent-relay.sh status     # Check if running

set -e

LABEL="com.hsem.agent-relay"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG="$HOME/Library/Logs/${LABEL}.log"
APP_SUPPORT="$HOME/Library/Application Support"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RELAY_SRC="$SCRIPT_DIR/start-agent-relay.py"
RELAY_DST="$APP_SUPPORT/hsem-agent-relay.py"
TEMPLATE="$SCRIPT_DIR/com.hsem.agent-relay.plist.template"

install_service() {
    echo "Installing HSEM agent relay as LaunchAgent..."

    if [ ! -f "$RELAY_SRC" ]; then
        echo "ERROR: Relay script not found at $RELAY_SRC"
        exit 1
    fi

    # Copy relay to Application Support (launchd can't access arbitrary paths)
    mkdir -p "$APP_SUPPORT"
    cp "$RELAY_SRC" "$RELAY_DST"

    # Create LaunchAgents dir if needed
    mkdir -p "$HOME/Library/LaunchAgents"

    # Generate plist from template
    sed -e "s|RELAY_SCRIPT_PATH|$RELAY_DST|g" \
        -e "s|LOG_PATH|$LOG|g" \
        -e "s|SSH_AUTH_SOCK_PLACEHOLDER|${SSH_AUTH_SOCK:-$HOME/.gnupg/S.gpg-agent.ssh}|g" \
        "$TEMPLATE" > "$PLIST"

    # Unload old if running
    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true

    # Load and start
    launchctl bootstrap "gui/$(id -u)" "$PLIST"

    echo ""
    echo "Installed. Service will start at login and run in the background."
    echo "  Relay script: $RELAY_DST"
    echo "  Plist:        $PLIST"
    echo "  Logs:         $LOG"
    echo ""
    echo "Ports: 9999 (SSH agent), 9998 (GPG agent), 9997 (scdaemon)"
}

uninstall_service() {
    echo "Uninstalling HSEM agent relay..."

    launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
    rm -f "$PLIST"
    rm -f "$LOG"
    rm -f "$RELAY_DST"

    echo "Uninstalled."
}

status_service() {
    if launchctl print "gui/$(id -u)/${LABEL}" > /dev/null 2>&1; then
        echo "Service: RUNNING"
        launchctl print "gui/$(id -u)/${LABEL}" 2>/dev/null | grep -E "state|last exit"
        echo ""
        lsof -iTCP -sTCP:LISTEN -P 2>/dev/null | grep -E "999[789]" || echo "No ports listening (may need a moment to start)"
    else
        echo "Service: NOT INSTALLED"
    fi
}

case "${1:-}" in
    install) install_service ;;
    uninstall) uninstall_service ;;
    status) status_service ;;
    *)
        echo "Usage: $0 {install|uninstall|status}"
        exit 1
        ;;
esac