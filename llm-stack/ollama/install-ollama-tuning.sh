#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="${SCRIPT_DIR}/systemd/override.conf"
TARGET_DIR="/etc/systemd/system/ollama.service.d"
TARGET_FILE="${TARGET_DIR}/override.conf"

if ! command -v systemctl >/dev/null 2>&1; then
    echo "Error: systemctl is required but was not found." >&2
    exit 1
fi

if [ ! -f "${SOURCE_FILE}" ]; then
    echo "Error: missing source file: ${SOURCE_FILE}" >&2
    exit 1
fi

if ! systemctl cat ollama.service >/dev/null 2>&1; then
    echo "Error: ollama.service was not found. Install Ollama before applying tuning." >&2
    exit 1
fi

echo "Installing Ollama systemd tuning from ${SOURCE_FILE}"
sudo mkdir -p "${TARGET_DIR}"
sudo install -m 0644 "${SOURCE_FILE}" "${TARGET_FILE}"

echo "Reloading systemd and restarting Ollama..."
sudo systemctl daemon-reload
sudo systemctl restart ollama

echo
echo "Effective Ollama environment:"
systemctl show ollama --property=Environment

echo
echo "Ollama service status:"
systemctl --no-pager --full status ollama
