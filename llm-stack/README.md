# Local LLM Stack

Stack local con:

- Ollama en el host
- Adapter FastAPI con API tipo OpenAI
- Open WebUI detrás de Caddy
- Prometheus + Grafana
- SQLite para API keys y prioridades
- Almacenamiento protegido de API keys con secreto y salt

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
- `API_KEY_SECRET`
- `API_KEY_SALT`
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
El fichero real `.env` debe existir localmente y no se sube a git.

## API keys

Las API keys ya no deben almacenarse en claro. El adapter deriva un valor determinista protegido a partir de:

- `API_KEY_SECRET`
- `API_KEY_SALT`

Ese valor derivado es el que se guarda en SQLite y el que se usa para lookup en cada request.

La consecuencia práctica es:

- la base no contiene la API key original
- el adapter necesita esas dos variables de entorno para validar requests
- si cambias `API_KEY_SECRET` o `API_KEY_SALT`, las keys ya almacenadas dejarán de coincidir hasta que las recrees o migres con los mismos valores

Base de datos: `data/auth.db`

Tabla:

```sql
CREATE TABLE api_keys (
  key_hash TEXT PRIMARY KEY,
  priority TEXT NOT NULL
);
```

Crear una key nueva de forma interactiva:

```bash
cd /home/tupolev/llm-stack
export AUTH_DB_PATH=/home/tupolev/llm-stack/data/auth.db
export API_KEY_SECRET='cambia-esto'
export API_KEY_SALT='cambia-esto-tambien'
python3 adapter/manage_api_keys.py create --priority high
```

Listar hashes almacenados:

```bash
python3 adapter/manage_api_keys.py list
```

Migrar una base antigua que todavía tenga la columna `key` en claro:

```bash
python3 adapter/manage_api_keys.py migrate-legacy
```

## Uso rápido

Levantar toda la pila:

```bash
cp .env.example .env
# editar .env con valores reales
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

Notas sobre configuración:

- el servicio `adapter` carga `.env` mediante `env_file`
- `.env.example` es solo plantilla; el `.env` real debe contener los secretos

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
- Si cambias `API_KEY_SECRET` o `API_KEY_SALT`, tendrás que recrear o migrar las API keys con esos mismos valores.
