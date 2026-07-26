FROM python:3.14.2-alpine

ENV SSH_AUTH_SOCK=/tmp/ssh-agent.sock
ENV GNUPGHOME=/tmp/gpg-home

# Install GPG, smartcard support, and YubiKey manager
# pyscard (ykman dep) needs gcc/musl-dev/pcsc-lite-dev/swig to build
RUN apk add --no-cache \
    gnupg \
    gpg-agent \
    pinentry \
    pcsc-lite \
    pcsc-lite-dev \
    openssh-client \
    git \
    socat \
    bash \
    nodejs \
    npm \
    gcc \
    musl-dev \
    swig \
    && pip install --no-cache-dir uv yubikey-manager \
    && apk del pcsc-lite-dev gcc musl-dev swig

# Copy the SSH agent bridge script
COPY scripts/devcontainer-agent-bridge.sh /usr/local/bin/devcontainer-agent-bridge.sh
RUN chmod +x /usr/local/bin/devcontainer-agent-bridge.sh
