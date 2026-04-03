#!/bin/bash

echo "Waiting for Ollama..."

until curl -s http://localhost:11434/api/tags > /dev/null; do
  sleep 2
done

echo "Ollama ready. Warming up..."

/home/tupolev/warm-ollama.sh
