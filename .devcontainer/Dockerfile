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
    shellcheck \
    gcc \
    libc6-dev \
    swig \
    curl \
    && pip install --no-cache-dir uv yubikey-manager \
    && apt-get purge -y libpcsclite-dev gcc libc6-dev swig \
    && apt-get autoremove -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# rtk installation
RUN curl -fsSL https://raw.githubusercontent.com/rtk-ai/rtk/refs/heads/master/install.sh | sh
RUN echo 'export PATH="$HOME/.local/bin:$PATH"' >> /root/.bashrc

# Copy devcontainer helper scripts
COPY scripts/devcontainer-agent-bridge.sh /usr/local/bin/devcontainer-agent-bridge.sh
COPY scripts/setup-python-deps.sh /usr/local/bin/setup-python-deps.sh
COPY scripts/post-create.sh /usr/local/bin/post-create.sh
COPY scripts/post-start.sh /usr/local/bin/post-start.sh
COPY scripts/check-agent-bridge.sh /usr/local/bin/check-agent-bridge.sh
RUN chmod +x /usr/local/bin/devcontainer-agent-bridge.sh /usr/local/bin/setup-python-deps.sh \
    /usr/local/bin/post-create.sh /usr/local/bin/post-start.sh /usr/local/bin/check-agent-bridge.sh
RUN echo '. /usr/local/bin/check-agent-bridge.sh' >> /root/.bashrc
