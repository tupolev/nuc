# llama.cpp Runtime

The model server runs as the `llama-server` Docker Compose service using the
official Vulkan image. The adapter reaches its OpenAI-compatible API at
`http://llama-server:8080/v1` on the internal Compose network.

The server downloads and caches `ggml-org/Qwen3-8B-GGUF:Q8_0` in its managed
Docker volume. The configured alias is `qwen3:8b`.

Operate the runtime with Docker Compose:

```bash
cd /home/tupolev/llm-stack
docker compose up -d --wait llama-server
docker compose logs -f llama-server
```

The main `llm-stack.service` starts Compose with `--wait`, so no separate
model warmup service is required.
