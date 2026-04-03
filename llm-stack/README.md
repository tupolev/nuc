# Local LLM Stack

Stack local con:

- Ollama en el host
- Adapter FastAPI con API tipo OpenAI
- Open WebUI detrás de Caddy
- Prometheus + Grafana
- SQLite para API keys y prioridades

## Arquitectura

```text
Cliente
  -> Caddy (https://nuc.fritz.box)
    -> /v1/* -> adapter:4000
    -> /metrics -> adapter:4000
    -> /metrics/* -> adapter:4000
    -> /* -> open-webui:8080

adapter
  -> Ollama en host.docker.internal:11434
  -> /data/auth.db
  -> /data/files

Prometheus
  -> scrape adapter:4000/metrics/prometheus
  -> scrape ollama-exporter:9101
  -> scrape host.docker.internal:9100
```

## Endpoints útiles

- API OpenAI-compatible directa: `http://localhost:4000`
- API OpenAI-compatible vía Caddy: `https://nuc.fritz.box/v1/...`
- Open WebUI: `https://nuc.fritz.box/`
- Métricas JSON del adapter: `https://nuc.fritz.box/metrics`
- Métricas Prometheus del adapter: `https://nuc.fritz.box/metrics/prometheus`
- Prometheus UI: `http://localhost:9090`
- Grafana: `http://localhost:3001`

## Funcionalidad actual del adapter

- `POST /v1/chat/completions`
- `POST /v1/embeddings`
- `GET /v1/models`
- `GET /v1/tools`
- `POST /v1/tools/web_search`
- `POST /v1/tools/python`
- `POST /v1/tools/save_file`
- `GET /v1/openapi.json`
- `GET /metrics`
- `GET /metrics/prometheus`

Además soporta:

- Streaming SSE en chat
- Colas con prioridad `high` / `medium` / `low`
- Modo tools `server`
- Modo tools `client`
- Traducción de `tool_calls` entre formato OpenAI y Ollama
- Métricas de latencia, colas, streaming y tools

## Variables del adapter

El adapter lee estas variables:

- `OLLAMA_URL`
- `FILES_DIR`
- `AUTH_DB_PATH`
- `CHAT_CONCURRENCY`
- `EMBED_CONCURRENCY`
- `QUEUE_TIMEOUT`
- `TOOL_MAX_ITERATIONS`
- `TOOL_ARG_MAX_LEN`
- `TOOL_OUTPUT_MAX_LEN`
- `PYTHON_TIMEOUT`
- `PYTHON_CODE_MAX_LEN`
- `DEFAULT_MODEL`
- `TOOL_EXECUTION_MODE`
- `AUTO_ENABLE_LOCAL_TOOLS`

Hay una plantilla en [.env.example](/home/tupolev/llm-stack/.env.example).

## API keys

Base de datos: `data/auth.db`

Tabla:

```sql
CREATE TABLE api_keys (
  key TEXT PRIMARY KEY,
  priority TEXT
);
```

Ejemplos:

```sql
INSERT INTO api_keys VALUES ('maravillas123', 'medium');
INSERT INTO api_keys VALUES ('admin123', 'high');
INSERT INTO api_keys VALUES ('batch123', 'low');
```

## Uso rápido

Levantar toda la pila:

```bash
docker compose up -d
```

Reconstruir solo el adapter cuando cambie `adapter/app.py`:

```bash
docker compose build adapter
docker compose up -d adapter
```

Recargar Caddy cuando cambie `Caddyfile`:

```bash
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

## Ejemplos de API

Listar modelos:

```bash
curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer maravillas123"
```

Chat simple:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer maravillas123" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

Chat streaming:

```bash
curl -N http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer maravillas123" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "stream": true,
    "messages": [
      {"role":"user","content":"Describe this server in one sentence."}
    ]
  }'
```

Client tools:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer maravillas123" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "tool_execution_mode": "client",
    "tools": [
      {
        "type": "function",
        "function": {
          "name": "python",
          "description": "Run Python",
          "parameters": {
            "type": "object",
            "properties": {
              "code": {"type": "string"}
            },
            "required": ["code"]
          }
        }
      }
    ],
    "messages": [
      {"role":"user","content":"Use python to compute 19*23."}
    ]
  }'
```

Prioridad explícita:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer admin123" \
  -H "X-Priority: high" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'
```

Embeddings:

```bash
curl http://localhost:4000/v1/embeddings \
  -H "Authorization: Bearer batch123" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "all-minilm:latest",
    "input": "hello world"
  }'
```

Prometheus metrics:

```bash
curl http://localhost:4000/metrics/prometheus
```

## Verificación

Desde `/home/tupolev`:

```bash
BASE_URL="https://nuc.fritz.box" API_KEY="maravillas123" bash e2e.sh -vv
```

Estado validado durante esta puesta al día:

```text
Summary: total=6 ok=6 fail=0
```

## Notas operativas

- Open WebUI no expone la API OpenAI-compatible de esta pila; esa la sirve el `adapter`.
- Prometheus y Grafana están pensados principalmente para tráfico interno de la propia pila.
- Si cambias rutas públicas del adapter, probablemente tendrás que ajustar también `Caddyfile`.
