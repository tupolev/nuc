#!/usr/bin/env bash

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run this script with sudo." >&2
    exit 1
fi

STACK_DIR="/home/tupolev/llm-stack"
UNIT_SOURCE="${STACK_DIR}/systemd/llm-stack.service"

install -m 0644 "$UNIT_SOURCE" /etc/systemd/system/llm-stack.service

systemctl daemon-reload
systemctl enable --now llm-stack.service

echo "llama.cpp systemd unit refreshed."
