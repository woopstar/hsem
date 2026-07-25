FROM python:3.14.2-alpine

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
    gcc \
    musl-dev \
    swig \
    && pip install --no-cache-dir yubikey-manager \
    && apk del pcsc-lite-dev gcc musl-dev swig

# Copy the SSH agent bridge script
COPY scripts/devcontainer-agent-bridge.sh /usr/local/bin/devcontainer-agent-bridge.sh
RUN chmod +x /usr/local/bin/devcontainer-agent-bridge.sh