#!/bin/bash

MODELS=(
  "qwen2.5-coder:14b"
  "qwen2.5-coder:7b"
  "llama3-groq-tool-use:8b"
  "llama3.1:8b"
  "qwen3-coder"
  "deepseek-coder-v2:16b"
)

echo "Waiting for Ollama..."

until curl -s http://localhost:11434/api/tags > /dev/null; do
  sleep 2
done

echo "Ollama ready. Warming up..."

for model in "${MODELS[@]}"; do
  /home/tupolev/warm-ollama.sh "$model"
done

curl http://localhost:11434/api/embeddings \
  -d '{"model":"all-minilm","prompt":"warmup"}'