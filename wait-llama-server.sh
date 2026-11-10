#!/usr/bin/env bash

set -euo pipefail

STACK_DIR="${STACK_DIR:-/home/tupolev/llm-stack}"
cd "$STACK_DIR"
exec docker compose up -d --wait llama-server
