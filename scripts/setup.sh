#!/usr/bin/env sh

apk add --no-cache bash

cd "$(dirname "$0")/.." || exit

python3 -m pip install --requirement requirements.txt
