#!/usr/bin/env python3
"""Start TCP relays for SSH agent, GPG agent, and scdaemon on macOS host.

Docker Desktop for Mac cannot forward Unix domain sockets across the VM boundary.
This script listens on TCP ports and forwards traffic to the Unix sockets,
allowing the devcontainer to reach the YubiKey-backed agents.

Usage:
    python3 start-agent-relay.py
"""

import argparse
import os
import socket
import select
import sys
import threading


def relay(client_sock: socket.socket, agent_path: str) -> None:
    """Bidirectional relay between a client socket and the Unix agent socket."""
    agent_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        agent_sock.connect(agent_path)
    except OSError as e:
        print(f"Failed to connect to agent at {agent_path}: {e}", file=sys.stderr)
        client_sock.close()
        return

    sockets = [client_sock, agent_sock]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                break
            for sock in readable:
                data = sock.recv(65536)
                if not data:
                    return
                target = agent_sock if sock is client_sock else client_sock
                target.sendall(data)
    finally:
        client_sock.close()
        agent_sock.close()


def start_listener(port: int, socket_path: str, label: str) -> None:
    """Start a TCP listener that relays to a Unix socket."""
    if not os.path.exists(socket_path):
        print(f"ERROR: {label} socket not found: {socket_path}", file=sys.stderr)
        return

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(5)

    print(f"{label} relay: 127.0.0.1:{port} -> {socket_path}")

    def accept_loop():
        try:
            while True:
                client, addr = server.accept()
                t = threading.Thread(target=relay, args=(client, socket_path), daemon=True)
                t.start()
        except OSError:
            pass

    threading.Thread(target=accept_loop, daemon=True).start()


def main() -> None:
    gnupg_dir = os.path.expanduser("~/.gnupg")

    parser = argparse.ArgumentParser(description="SSH + GPG + scdaemon agent TCP relays")
    parser.add_argument("--ssh-port", type=int, default=9999, help="TCP port for SSH agent relay")
    parser.add_argument("--gpg-port", type=int, default=9998, help="TCP port for GPG agent relay")
    parser.add_argument("--scdaemon-port", type=int, default=9997, help="TCP port for scdaemon relay")
    parser.add_argument(
        "--ssh-socket",
        default=os.environ.get("SSH_AUTH_SOCK", os.path.join(gnupg_dir, "S.gpg-agent.ssh")),
        help="Path to SSH agent Unix socket",
    )
    parser.add_argument(
        "--gpg-socket",
        default=os.path.join(gnupg_dir, "S.gpg-agent"),
        help="Path to GPG agent Unix socket",
    )
    parser.add_argument(
        "--scdaemon-socket",
        default=os.path.join(gnupg_dir, "S.scdaemon"),
        help="Path to scdaemon Unix socket",
    )
    args = parser.parse_args()

    start_listener(args.ssh_port, args.ssh_socket, "SSH agent")
    start_listener(args.gpg_port, args.gpg_socket, "GPG agent")
    start_listener(args.scdaemon_port, args.scdaemon_socket, "scdaemon")

    print("")
    print("Container can connect to:")
    print(f"  SSH agent: host.docker.internal:{args.ssh_port}")
    print(f"  GPG agent: host.docker.internal:{args.gpg_port}")
    print(f"  scdaemon:  host.docker.internal:{args.scdaemon_port}")
    print("Press Ctrl+C to stop.")

    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()