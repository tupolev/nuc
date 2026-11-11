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
# Local models

The NUC runs the following local services on the private Docker network:

| Capability | Model | Service | API |
| --- | --- | --- | --- |
| Code and tools | Qwen3 8B Q8_0 | `llama-server` | OpenAI-compatible `/v1` via `adapter` |
| Image analysis | Qwen2.5-VL 3B Q4_K_M | `vision-server` | OpenAI-compatible `http://vision-server:8081/v1` |
| Image generation | Stable Diffusion 1.5 Q4_K | `image-server` | `http://image-server:7860` |
| Speech to text | faster-whisper-small | `speaches` | OpenAI-compatible `http://speaches:8000/v1` |
| Text to speech | Kokoro-82M | `speaches` | OpenAI-compatible `http://speaches:8000/v1` |

All external clients use the authenticated endpoint in `$NUC_BASE_URL`, with their API key. The adapter routes requests without exposing service ports directly:

| Capability | Public route |
| --- | --- |
| Vision | `POST /v1/chat/completions` with `model: "qwen2.5-vl:3b"` |
| Image generation or edits | `POST /v1/images/generations` or `POST /v1/images/edits` |
| Speech to text | `POST /v1/audio/transcriptions` or `POST /v1/audio/translations` |
| Text to speech | `POST /v1/audio/speech` |

The container ports remain private to Docker. This lets OpenCode and other OpenAI-compatible clients share the same TLS endpoint and API key without publishing unauthenticated services.

The first startup downloads the Qwen vision model. Speech models are downloaded explicitly after Speaches is running:

```bash
curl -X POST http://127.0.0.1:8000/v1/models/Systran/faster-whisper-small
curl -X POST http://127.0.0.1:8000/v1/models/speaches-ai/Kokoro-82M-v1.0-ONNX
```

The Stable Diffusion weight is stored at `models/stable-diffusion/v1-5-pruned_Q4_K.gguf` and is intentionally ignored by Git.

## Using the public API

All examples use the same NUC API key and the certificate authority installed for
the NUC. On a client without the CA installed, set `NUC_CA_CERT` to the mkcert
root certificate and retain `--cacert`; omit that option once the CA is trusted
by the operating system.

```bash
export NUC_BASE_URL=https://llm.example.internal/v1
export NUC_LLM_API_KEY='replace-with-your-key'
export NUC_CA_CERT="$HOME/.config/opencode/certs/nuc-mkcert-rootCA.pem"

curl --fail-with-body --cacert "$NUC_CA_CERT" \
  -H "Authorization: Bearer $NUC_LLM_API_KEY" \
  "$NUC_BASE_URL/models"
```

### Code and tool calling: Qwen3 8B

Use `qwen3:8b` for programming, general chat, and OpenAI function calling.
OpenCode should continue to use this model and the existing `nuc` provider.

```bash
curl --fail-with-body --cacert "$NUC_CA_CERT" \
  -H "Authorization: Bearer $NUC_LLM_API_KEY" \
  -H 'Content-Type: application/json' \
  "$NUC_BASE_URL/chat/completions" \
  -d '{
    "model": "qwen3:8b",
    "messages": [{"role": "user", "content": "Explain this Python traceback."}]
  }'
```

### Image analysis: Qwen2.5-VL 3B

Use `qwen2.5-vl:3b` with OpenAI multimodal message content. It is intended for
describing images, extracting visible information, and visual Q&A, not image
generation.

```bash
{
  printf '%s' '{"model":"qwen2.5-vl:3b","messages":[{"role":"user","content":[{"type":"text","text":"Describe this image in Spanish."},{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,'
  base64 < photo.jpg | tr -d '\n'
  printf '%s\n' '"}}]}]}'
} > vision-request.json

curl --fail-with-body --cacert "$NUC_CA_CERT" \
  -H "Authorization: Bearer $NUC_LLM_API_KEY" \
  -H 'Content-Type: application/json' \
  --data-binary @vision-request.json "$NUC_BASE_URL/chat/completions"
```

### Image generation: Stable Diffusion 1.5

Stable Diffusion exposes the OpenAI image API. Start with `512x512` and one
image on this integrated GPU; higher resolutions and batches consume much more
shared memory and take longer.

```bash
curl --fail-with-body --cacert "$NUC_CA_CERT" \
  -H "Authorization: Bearer $NUC_LLM_API_KEY" \
  -H 'Content-Type: application/json' \
  "$NUC_BASE_URL/images/generations" \
  -d '{
    "prompt": "A small modern house in a green forest, architectural photograph",
    "size": "512x512",
    "n": 1,
    "response_format": "b64_json"
  }' \
  | jq -r '.data[0].b64_json' | base64 -d > generated.png
```

`POST /v1/images/edits` is also available for multipart OpenAI image-editing
requests. The service uses the local Stable Diffusion 1.5 Q4_K model.

### Speech to text: faster-whisper-small

Use the standard OpenAI transcription endpoint. `faster-whisper-small` is
multilingual, including Spanish. Supply `language` when it is known for more
predictable transcription.

```bash
curl --fail-with-body --cacert "$NUC_CA_CERT" \
  -H "Authorization: Bearer $NUC_LLM_API_KEY" \
  -F 'model=Systran/faster-whisper-small' \
  -F 'language=es' \
  -F 'response_format=json' \
  -F 'file=@recording.m4a' \
  "$NUC_BASE_URL/audio/transcriptions"
```

Use `POST /v1/audio/translations` with the same model to translate speech to
English.

### Text to speech: Kokoro-82M

Kokoro-82M runs locally on CPU. `ef_dora` is its Spanish female voice; other
Spanish voices include `em_alex` and `em_santa`.

```bash
curl --fail-with-body --cacert "$NUC_CA_CERT" \
  -H "Authorization: Bearer $NUC_LLM_API_KEY" \
  -H 'Content-Type: application/json' \
  "$NUC_BASE_URL/audio/speech" \
  -d '{
    "model": "speaches-ai/Kokoro-82M-v1.0-ONNX",
    "voice": "ef_dora",
    "input": "Hola. Esta voz se genera localmente en el NUC.",
    "response_format": "mp3"
  }' > speech.mp3
```

## Open WebUI

Open WebUI is preconfigured in `docker-compose.yml` to use the authenticated
adapter over the private Compose network. No additional exposed ports, TLS
exceptions, or API keys are needed in the browser.

The following capabilities are enabled in the Open WebUI interface:

| Interface feature | Local service | Configuration |
| --- | --- | --- |
| Chat and code | Qwen3 8B | Select `qwen3:8b` |
| Image analysis | Qwen2.5-VL 3B | Select `qwen2.5-vl:3b`, then attach an image |
| Image generation and edits | Stable Diffusion 1.5 | Image generation is enabled with model `sd-cpp-local` |
| Dictation and transcription | faster-whisper-small | OpenAI-compatible STT with multipart audio uploads |
| Read aloud | Kokoro-82M | Voice `ef_dora` by default |

The equivalent container settings are `IMAGE_GENERATION_ENGINE=openai`,
`AUDIO_STT_ENGINE=openai`, and `AUDIO_TTS_ENGINE=openai`; each points to
`http://adapter:4000/v1` with `OPENWEBUI_ADAPTER_API_KEY`. If an administrator
changes these values in Open WebUI's Admin Panel, that persisted configuration
can override the Compose defaults.

## Operations

Check service status and follow a model's startup/download logs from the NUC:

```bash
cd /home/tupolev/llm-stack
docker compose ps
docker compose logs -f vision-server image-server speaches
```

The image and vision services share the integrated GPU with Qwen3. They can
coexist in memory, but run one GPU-heavy request at a time for predictable
latency.
