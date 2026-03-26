#!/usr/bin/env bash

OLLAMA_BIN=/usr/local/bin/ollama
CURL_BIN=/usr/bin/curl

export OLLAMA_NUM_PARALLEL=2

until $CURL_BIN -s http://localhost:11434/api/tags >/dev/null; do
    sleep 2
done

$OLLAMA_BIN run qwen2.5-coder:7b "warmup" >/dev/null 2>&1

curl http://localhost:11434/api/embeddings \
-d '{"model":"all-minilm","prompt":"warmup"}'
