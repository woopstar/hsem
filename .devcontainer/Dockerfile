FROM python:3.14.2-slim

ENV SSH_AUTH_SOCK=/tmp/ssh-agent.sock
ENV GNUPGHOME=/tmp/gpg-home
ENV WORKSPACE_DIR=/workspaces/hsem

# Install GPG, smartcard support, YubiKey manager, and dev tooling
RUN apt-get update && apt-get install -y --no-install-recommends \
    gnupg \
    gpg-agent \
    pinentry-curses \
    pcscd \
    pcsc-tools \
    libpcsclite-dev \
    openssh-client \
    git \
    socat \
    bash \
    nodejs \
    npm \
    shellcheck \
    gcc \
    libc6-dev \
    swig \
    && pip install --no-cache-dir uv yubikey-manager \
    && apt-get purge -y libpcsclite-dev gcc libc6-dev swig \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy devcontainer helper scripts
COPY scripts/devcontainer-agent-bridge.sh /usr/local/bin/devcontainer-agent-bridge.sh
COPY scripts/setup-python-deps.sh /usr/local/bin/setup-python-deps.sh
RUN chmod +x /usr/local/bin/devcontainer-agent-bridge.sh /usr/local/bin/setup-python-deps.sh
