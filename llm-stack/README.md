# Local LLM Stack

Stack local para un NUC con:

- Ollama en el host
- adapter FastAPI con API compatible con OpenAI
- Open WebUI detrás de Caddy
- Prometheus y Grafana
- SQLite para API keys y prioridades

Este documento está pensado para la persona que instala, configura y opera el proyecto.
Si lo que necesitas es contexto profundo para un agente de IA, consulta [`PRD.md`](/home/tupolev/llm-stack/PRD.md).

## Qué hace

La pila expone una API tipo OpenAI para clientes y agentes, una interfaz web con Open WebUI y métricas locales para observabilidad.

Rutas principales:

- `https://nuc.fritz.box/v1/...` -> adapter
- `https://nuc.fritz.box/` -> Open WebUI
- `https://nuc.fritz.box/metrics` -> métricas JSON del adapter
- `https://nuc.fritz.box/metrics/prometheus` -> métricas Prometheus del adapter

## Arquitectura

```text
Cliente
  -> Caddy
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

## Requisitos

- Docker y Docker Compose
- Ollama accesible desde el host
- certificados y configuración de Caddy válidos para `nuc.fritz.box`
- un fichero `.env` local con los valores reales

## Configuración

Hay una plantilla en [` .env.example `](/home/tupolev/llm-stack/.env.example).

Preparación inicial:

```bash
cd /home/tupolev/llm-stack
cp .env.example .env
```

Después edita `.env` y rellena al menos:

- `OLLAMA_URL`
- `AUTH_DB_PATH`
- `FILES_DIR`
- `API_KEY_SECRET`
- `API_KEY_SALT`
- `DEFAULT_MODEL`

Notas importantes:

- `.env` no debe subirse a git
- `adapter` carga su configuración usando `env_file`
- si cambias `API_KEY_SECRET` o `API_KEY_SALT`, las API keys ya creadas dejarán de coincidir

## Levantar la pila

```bash
cd /home/tupolev/llm-stack
docker compose up -d
```

Servicios principales:

- `adapter`
- `open-webui`
- `caddy`
- `prometheus`
- `grafana`
- `ollama-exporter`
- `node-exporter`

## Operación habitual

Reconstruir el adapter tras cambios en Python:

```bash
cd /home/tupolev/llm-stack
docker compose build adapter
docker compose up -d adapter
```

Recargar Caddy tras cambios en [`Caddyfile`](/home/tupolev/llm-stack/Caddyfile):

```bash
cd /home/tupolev/llm-stack
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

Ver el estado de los servicios:

```bash
docker compose ps
```

Ver logs del adapter:

```bash
docker compose logs -f adapter
```

## API keys

Las API keys no se almacenan en claro. El adapter guarda un `key_hash` derivado a partir de:

- `API_KEY_SECRET`
- `API_KEY_SALT`

La base de datos esperada es `data/auth.db` y la tabla es:

```sql
CREATE TABLE api_keys (
  key_hash TEXT PRIMARY KEY,
  priority TEXT NOT NULL
);
```

Crear una API key nueva:

```bash
cd /home/tupolev/llm-stack
export AUTH_DB_PATH=/home/tupolev/llm-stack/data/auth.db
export API_KEY_SECRET='cambia-esto'
export API_KEY_SALT='cambia-esto-tambien'
python3 adapter/manage_api_keys.py create --priority high
```

Listar las keys almacenadas:

```bash
python3 adapter/manage_api_keys.py list
```

Migrar una base antigua con columna `key` en claro:

```bash
python3 adapter/manage_api_keys.py migrate-legacy
```

## Endpoints útiles

- API directa del adapter: `http://localhost:4000`
- API vía Caddy: `https://nuc.fritz.box/v1/...`
- Open WebUI: `https://nuc.fritz.box/`
- Prometheus UI: `http://localhost:9090`
- Grafana: `http://localhost:3001`

## Ejemplos rápidos

Listar modelos:

```bash
curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer TU_API_KEY"
```

Chat simple:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer TU_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

Streaming:

```bash
curl -N http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer TU_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "stream": true,
    "messages": [
      {"role":"user","content":"Describe this server in one sentence."}
    ]
  }'
```

## Validación end-to-end

El script recomendado es [`/home/tupolev/e2e.sh`](/home/tupolev/e2e.sh).

Está pensado para ejecutarse desde la máquina cliente donde corre el agente, no desde el NUC, y solo necesita:

- `BASE_URL`
- `API_KEY`

Ejemplo:

```bash
BASE_URL="https://nuc.fritz.box" API_KEY="TU_API_KEY" bash /home/tupolev/e2e.sh -vv
```

## Qué incluye hoy el adapter

Capacidades principales:

- chat completions
- embeddings
- model listing
- tools OpenAI-style
- streaming SSE
- modo `server` y modo `client` para tools
- métricas JSON y Prometheus

Tools disponibles:

- `web_search`
- `python`
- `save_file`
- `fetch_url`
- `weather`
- `time`
- `calendar`
- `news_search`
- `geocode`
- `http_request`
- `sqlite_query`
- `shell_safe`
- `calendar_events`

## Troubleshooting rápido

- Si cambias código en `adapter/app.py`, reconstruye y reinicia `adapter`.
- Si cambias el proxy, recarga Caddy.
- Si una API key deja de funcionar, revisa primero `API_KEY_SECRET`, `API_KEY_SALT` y `data/auth.db`.
- Si una tool funciona mal desde un cliente, prueba antes el endpoint directo en `/v1/tools/...`.
- Si el agente falla y no sabes si es problema del cliente o del stack, ejecuta `e2e.sh`.
