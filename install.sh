#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e '.[browser]'
.venv/bin/python -m playwright install chromium
if [ ! -e config.yaml ]; then
    cp config.example.yaml config.yaml
    printf 'Created config.yaml from the example; edit your units before syncing.\n'
fi
printf '\nInstalled. Run: source .venv/bin/activate && usync check\n'
