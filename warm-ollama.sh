#!/usr/bin/env bash

OLLAMA_BIN=/usr/local/bin/ollama
CURL_BIN=/usr/bin/curl

export OLLAMA_NUM_PARALLEL=2

MODEL_NAME="${1:-}"

if [ -z "$MODEL_NAME" ]; then
    echo "Usage: $0 <model-name>" >&2
    exit 1
fi

until $CURL_BIN -s http://localhost:11434/api/tags >/dev/null; do
    sleep 2
done

echo "Warming model: $MODEL_NAME"
$OLLAMA_BIN run "$MODEL_NAME" "warmup" >/dev/null 2>&1
curl http://localhost:11434/api/embeddings \
-d '{"model":"all-minilm","prompt":"warmup"}'