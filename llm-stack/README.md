# Local LLM Stack

Local NUC stack with:

- Ollama on the host
- a FastAPI adapter exposing an OpenAI-compatible API
- Open WebUI behind Caddy
- Prometheus and Grafana
- SQLite for API keys and priorities

This document is for the human operator who installs, configures, and runs the project.
If you need deeper technical context for an AI agent, see [`PRD.md`](/home/tupolev/llm-stack/PRD.md).

## What It Does

The stack exposes an OpenAI-style API for clients and agents, a browser UI through Open WebUI, and local observability through metrics.

The adapter is now modularized for maintainability:

- [`app.py`](/home/tupolev/llm-stack/adapter/app.py): FastAPI composition and routes
- [`config.py`](/home/tupolev/llm-stack/adapter/config.py): environment variables and constants
- [`state.py`](/home/tupolev/llm-stack/adapter/state.py): auth DB, queues, and metrics
- [`tooling.py`](/home/tupolev/llm-stack/adapter/tooling.py): tools and tool registry
- [`openai_compat.py`](/home/tupolev/llm-stack/adapter/openai_compat.py): OpenAI/Ollama compatibility and tool calling

Main routes:

- `https://nuc.fritz.box/v1/...` -> adapter
- `https://nuc.fritz.box/` -> Open WebUI
- `https://nuc.fritz.box/metrics` -> adapter JSON metrics
- `https://nuc.fritz.box/metrics/prometheus` -> adapter Prometheus metrics

## Architecture

```text
Client
  -> Caddy
    -> /v1/* -> adapter:4000
    -> /metrics -> adapter:4000
    -> /metrics/* -> adapter:4000
    -> /* -> open-webui:8080

adapter
  -> Ollama on host.docker.internal:11434
  -> /data/auth.db
  -> /data/files

Prometheus
  -> scrape adapter:4000/metrics/prometheus
  -> scrape ollama-exporter:9101
  -> scrape host.docker.internal:9100
```

## Requirements

- Docker and Docker Compose
- Ollama reachable from the host
- valid Caddy certificates and config for `nuc.fritz.box`
- a local `.env` file with real values

## Configuration

A template is available in [` .env.example `](/home/tupolev/llm-stack/.env.example).

Initial setup:

```bash
cd /home/tupolev/llm-stack
cp .env.example .env
```

Then edit `.env` and fill in at least:

- `OLLAMA_URL`
- `AUTH_DB_PATH`
- `FILES_DIR`
- `API_KEY_SECRET`
- `API_KEY_SALT`
- `DEFAULT_MODEL`

Important notes:

- `.env` must not be committed to git
- the `adapter` service loads its configuration through `env_file`
- if you change `API_KEY_SECRET` or `API_KEY_SALT`, existing API keys will stop matching

## Start the Stack

```bash
cd /home/tupolev/llm-stack
docker compose up -d
```

Main services:

- `adapter`
- `open-webui`
- `caddy`
- `prometheus`
- `grafana`
- `ollama-exporter`
- `node-exporter`

## Day-to-Day Operations

Rebuild the adapter after Python changes:

```bash
cd /home/tupolev/llm-stack
docker compose build adapter
docker compose up -d adapter
```

This applies if you change [`app.py`](/home/tupolev/llm-stack/adapter/app.py) or any other Python module under [`adapter/`](/home/tupolev/llm-stack/adapter).

Reload Caddy after changes to [`Caddyfile`](/home/tupolev/llm-stack/Caddyfile):

```bash
cd /home/tupolev/llm-stack
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

Check service status:

```bash
docker compose ps
```

View adapter logs:

```bash
docker compose logs -f adapter
```

## API Keys

API keys are not stored in plaintext. The adapter stores a derived `key_hash` built from:

- `API_KEY_SECRET`
- `API_KEY_SALT`

The expected database is `data/auth.db` and the table is:

```sql
CREATE TABLE api_keys (
  key_hash TEXT PRIMARY KEY,
  priority TEXT NOT NULL
);
```

Create a new API key:

```bash
cd /home/tupolev/llm-stack
export AUTH_DB_PATH=/home/tupolev/llm-stack/data/auth.db
export API_KEY_SECRET='change-this'
export API_KEY_SALT='change-this-too'
python3 adapter/manage_api_keys.py create --priority high
```

List stored keys:

```bash
python3 adapter/manage_api_keys.py list
```

Migrate an older database that still has a plaintext `key` column:

```bash
python3 adapter/manage_api_keys.py migrate-legacy
```

## Useful Endpoints

- Direct adapter API: `http://localhost:4000`
- API through Caddy: `https://nuc.fritz.box/v1/...`
- Open WebUI: `https://nuc.fritz.box/`
- Prometheus UI: `http://localhost:9090`
- Grafana: `http://localhost:3001`

## Quick Examples

List models:

```bash
curl http://localhost:4000/v1/models \
  -H "Authorization: Bearer YOUR_API_KEY"
```

Simple chat:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

Streaming chat:

```bash
curl -N http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:7b",
    "stream": true,
    "messages": [
      {"role":"user","content":"Describe this server in one sentence."}
    ]
  }'
```

## End-to-End Validation

The recommended validation script is [`/home/tupolev/e2e.sh`](/home/tupolev/e2e.sh).

It is meant to run from the client machine where the agent runs, not from the NUC itself, and it only needs:

- `BASE_URL`
- `API_KEY`

Example:

```bash
BASE_URL="https://nuc.fritz.box" API_KEY="YOUR_API_KEY" bash /home/tupolev/e2e.sh -vv
```

## What the Adapter Currently Includes

Main capabilities:

- chat completions
- embeddings
- model listing
- OpenAI-style tools
- SSE streaming
- `server` and `client` tool modes
- JSON and Prometheus metrics

Available tools:

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

## Quick Troubleshooting

- If you change Python code under [`adapter/`](/home/tupolev/llm-stack/adapter), rebuild and restart `adapter`.
- If you change the proxy config, reload Caddy.
- If an API key stops working, check `API_KEY_SECRET`, `API_KEY_SALT`, and `data/auth.db` first.
- If a tool behaves incorrectly from a client, test the direct endpoint under `/v1/tools/...` first.
- If an agent fails and you are not sure whether the problem is the client or the stack, run `e2e.sh`.
