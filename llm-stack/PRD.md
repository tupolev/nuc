# Product Requirements Document

## Project Name

Local LLM Stack for NUC

## Document Purpose

This document is written for human operators and coding agents that need to understand, maintain, extend, or troubleshoot this project from zero context. It describes the goals, architecture, runtime model, security model, current capabilities, and known limitations of the stack.

## Executive Summary

This project runs a self-hosted LLM platform on a NUC. The stack exposes an OpenAI-compatible API through a custom FastAPI adapter placed in front of Ollama, serves a browser UI through Open WebUI, and includes observability with Prometheus and Grafana.

The adapter is the most important component. It translates between OpenAI-style requests and Ollama, enforces API-key-based access control, supports OpenAI-style tools/function calling, exposes utility tools directly over HTTP, and emits operational metrics.

The system is intended to support both:

- Human users interacting through Open WebUI.
- Console agents such as Codex, Claude Code, OpenCode, or similar tools that speak an OpenAI-compatible API and may rely on tool calling.

## Primary Goals

- Run a local, self-hosted LLM stack on a NUC.
- Expose an OpenAI-compatible API that works with existing agentic tooling.
- Support tool use in both server-executed and client-executed modes.
- Provide a practical set of tools for code agents: web search, shell inspection, HTTP fetches, weather, time, calendar utilities, SQLite queries, and file saving.
- Protect API keys better than plaintext storage in SQLite.
- Expose metrics suitable for local monitoring with Prometheus and Grafana.
- Keep deployment simple via Docker Compose and a local `.env` file.

## Non-Goals

- This is not a multi-tenant SaaS platform.
- This is not a hardened internet-facing production platform.
- This does not currently include a full identity or user-management system.
- This does not currently integrate with authenticated third-party calendars like Google Calendar or CalDAV.
- This does not currently guarantee perfect Google web-search parsing. The stack falls back to DuckDuckGo when needed.

## Target Users

### Human Operator

The main operator is a technically capable user managing the NUC and the Docker stack locally or over SSH. They need:

- Predictable rebuild and restart flows.
- Clear environment-variable configuration.
- Safe API key management.
- A way to inspect health and metrics.

### Console Coding Agent

The secondary user is an API-driven coding agent that needs:

- OpenAI-style `/v1/chat/completions`
- `/v1/models`
- function calling / tool calling
- SSE streaming
- tools for live information and local execution
- stable enough behavior to work from terminal-based agent clients

## High-Level Architecture

```text
Client / Agent
  -> Caddy
    -> /v1/*                -> adapter:4000
    -> /metrics             -> adapter:4000
    -> /metrics/*           -> adapter:4000
    -> everything else      -> open-webui:8080

adapter (FastAPI)
  -> Ollama on host.docker.internal:11434
  -> SQLite auth DB at /data/auth.db
  -> file storage at /data/files

Prometheus
  -> adapter:4000/metrics/prometheus
  -> ollama-exporter
  -> node-exporter on host

Grafana
  -> reads Prometheus as datasource
```

## Tech Stack

- Reverse proxy: Caddy
- LLM runtime: Ollama on the host
- API adapter: FastAPI / Python
- UI: Open WebUI
- Metrics: Prometheus
- Dashboards: Grafana
- Auth store: SQLite
- Packaging / orchestration: Docker Compose

## Repository Structure

Key files and folders:

- [`docker-compose.yml`](/home/tupolev/llm-stack/docker-compose.yml): orchestrates the stack
- [`Caddyfile`](/home/tupolev/llm-stack/Caddyfile): HTTPS reverse proxy and route mapping
- [`README.md`](/home/tupolev/llm-stack/README.md): operator-facing quick reference
- [`adapter/app.py`](/home/tupolev/llm-stack/adapter/app.py): core adapter implementation
- [`adapter/auth_security.py`](/home/tupolev/llm-stack/adapter/auth_security.py): API-key hashing, schema helpers, env requirements
- [`adapter/manage_api_keys.py`](/home/tupolev/llm-stack/adapter/manage_api_keys.py): CLI for managing protected API keys
- [`adapter/Dockerfile`](/home/tupolev/llm-stack/adapter/Dockerfile): adapter image build
- [`prometheus.yml`](/home/tupolev/llm-stack/prometheus.yml): Prometheus scrape config
- [`grafana/provisioning/datasources/datasource.yml`](/home/tupolev/llm-stack/grafana/provisioning/datasources/datasource.yml): Grafana datasource config
- [`grafana/provisioning/dashboards/dashboard.yml`](/home/tupolev/llm-stack/grafana/provisioning/dashboards/dashboard.yml): dashboard provisioning
- [`grafana/provisioning/dashboards/llm.json`](/home/tupolev/llm-stack/grafana/provisioning/dashboards/llm.json): dashboard definition
- [`/home/tupolev/e2e.sh`](/home/tupolev/e2e.sh): end-to-end validation script

## Runtime Components

### Adapter

The adapter is the system’s control plane. It is responsible for:

- OpenAI-compatible chat completions
- embeddings
- model listing
- tool catalog exposure
- tool execution in server mode
- tool-call translation in client mode
- queueing and priority handling
- API key authorization
- metrics

### Open WebUI

Open WebUI is the browser-facing interface. It is routed by Caddy for non-API paths.

### Ollama

Ollama runs on the host and serves model inference. The adapter communicates with Ollama using `host.docker.internal`.

### Observability Stack

Prometheus scrapes the adapter and exporters. Grafana reads from Prometheus to show dashboards.

## API Surface

### Core Endpoints

- `POST /v1/chat/completions`
- `POST /v1/embeddings`
- `GET /v1/models`
- `GET /v1/tools`
- `GET /v1/openapi.json`
- `GET /metrics`
- `GET /metrics/prometheus`

### Tool Endpoints

The adapter also exposes individual tool endpoints under `/v1/tools/...`. This is useful for direct testing and debugging.

Current tools:

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

## Tooling Model

The stack supports two tool-execution models.

### Server Mode

In `tool_execution_mode=server`, the adapter executes the tool itself and loops with the model until it can return a final answer.

Use cases:

- local utility tools
- agent clients that do not want to own tool execution
- simple server-side orchestration

### Client Mode

In `tool_execution_mode=client`, the adapter returns OpenAI-style `tool_calls`, allowing the external client or agent runtime to execute tools and feed the result back.

Use cases:

- Codex / Claude Code style orchestration
- clients that need full control over execution, auditing, or retries

## Current Tool Catalog

### `web_search`

Purpose:

- general web search for fresh information

Behavior:

- `provider=auto` tries Google first, then falls back to DuckDuckGo
- returns `attempted_providers`

Important limitation:

- Google’s HTML is not always stable or easily parseable; fallback to DuckDuckGo is expected behavior, not an anomaly

### `python`

Purpose:

- deterministic calculations
- local data processing
- small transformations that are better done by code than by the model alone

Important limitation:

- bounded by timeout and code-length limits

### `save_file`

Purpose:

- persist generated content to disk under the configured files directory

### `fetch_url`

Purpose:

- read a specific URL as raw text, cleaned text, or JSON

### `weather`

Purpose:

- retrieve current weather and a short forecast without relying on model memory

Implementation note:

- currently uses `wttr.in`

### `time`

Purpose:

- return current time based on timezone or UTC offset

### `calendar`

Purpose:

- generate month views and date-related utility answers

### `news_search`

Purpose:

- retrieve recent news in a more structured way than generic web search

Implementation note:

- currently uses Google News RSS

### `geocode`

Purpose:

- geocoding and reverse geocoding

Implementation note:

- currently uses OpenStreetMap / Nominatim

### `http_request`

Purpose:

- generic HTTP requests for JSON or text APIs

### `sqlite_query`

Purpose:

- inspect local SQLite databases from the adapter container

Important limitation:

- file paths are container paths, not host paths
- queries are intentionally bounded

### `shell_safe`

Purpose:

- allow restricted shell inspection commands from the adapter

Important limitation:

- only commands in the allowlist may run
- this is intentionally not arbitrary shell execution

### `calendar_events`

Purpose:

- parse events from ICS files or feeds

Important limitation:

- supports ICS inputs, not authenticated personal calendar integrations

## Authentication and Security Model

### Current Security Goal

API keys must not be stored in plaintext in `auth.db`.

### Current Implementation

The adapter stores a protected deterministic derived value named `key_hash` instead of the raw API key. For each incoming request:

1. The client sends the API key as a bearer token.
2. The adapter derives `key_hash` from that key using secret material from environment variables.
3. The adapter looks up `key_hash` in SQLite.
4. If found, the request is authorized and assigned a priority.

### Required Environment Variables

- `API_KEY_SECRET`
- `API_KEY_SALT`

These values must remain stable. If either value changes, previously stored hashes will no longer match the incoming API keys.

### Auth Database

Current expected schema:

```sql
CREATE TABLE api_keys (
  key_hash TEXT PRIMARY KEY,
  priority TEXT NOT NULL
);
```

### API Key Management CLI

The CLI entrypoint is [`manage_api_keys.py`](/home/tupolev/llm-stack/adapter/manage_api_keys.py).

Supported commands:

- `create`
- `list`
- `migrate-legacy`

Typical usage:

```bash
cd /home/tupolev/llm-stack
export AUTH_DB_PATH=/home/tupolev/llm-stack/data/auth.db
export API_KEY_SECRET='your-secret'
export API_KEY_SALT='your-salt'
python3 adapter/manage_api_keys.py create --priority high
```

## Configuration Model

### `.env`

The stack uses a local `.env` file. The real `.env` must not be committed. [`docker-compose.yml`](/home/tupolev/llm-stack/docker-compose.yml) loads it through `env_file`.

The template is [` .env.example `](/home/tupolev/llm-stack/.env.example).

Important variables include:

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
- `HTTP_TIMEOUT`
- `HTTP_MAX_BYTES`
- `SQLITE_QUERY_MAX_ROWS`
- `SHELL_TIMEOUT`
- `DEFAULT_MODEL`
- `TOOL_EXECUTION_MODE`
- `AUTO_ENABLE_LOCAL_TOOLS`
- `SAFE_SHELL_COMMANDS`

## Deployment and Operations

### First Run

```bash
cd /home/tupolev/llm-stack
cp .env.example .env
# edit .env with real values
docker compose up -d
```

### Rebuild Adapter After Python Changes

If [`app.py`](/home/tupolev/llm-stack/adapter/app.py) or other adapter code changes, rebuild and restart the adapter:

```bash
cd /home/tupolev/llm-stack
docker compose build adapter
docker compose up -d adapter
```

### Reload Caddy After Proxy Changes

If [`Caddyfile`](/home/tupolev/llm-stack/Caddyfile) changes:

```bash
cd /home/tupolev/llm-stack
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

## Monitoring and Metrics

The adapter exposes:

- `/metrics` for JSON metrics
- `/metrics/prometheus` for Prometheus scrape format

Metrics include:

- request counts
- active chat/embed requests
- queue depth
- errors
- streamed tokens
- wait-time and latency series
- tool request counts
- tool status/error counts
- tool latencies
- tool loop iterations

## End-to-End Validation

The canonical smoke and regression script is [`/home/tupolev/e2e.sh`](/home/tupolev/e2e.sh).

Design requirement:

- `e2e.sh` is intended to run from the client machine where an external agent lives, not from the NUC itself
- it must not depend on the NUC’s local `.env` file or shell environment
- it should behave as a black-box API test, using only the public base URL and an API key provided by the caller

The script now checks:

- `/v1/models`
- streaming chat
- tool calling in server mode
- tool calling in client mode, including round-trip tool result submission
- tool catalog
- direct tool endpoints for weather, time, calendar, fetch URL, news, geocoding, HTTP requests, safe shell, and web search
- `/v1/openapi.json`
- unauthorized access rejection
- Prometheus metrics

Typical usage:

```bash
BASE_URL="https://nuc.fritz.box" API_KEY="your-key" bash /home/tupolev/e2e.sh -vv
```

## Current Status

As of this document:

- the adapter is feature-rich enough to support console coding agents
- the stack exposes an OpenAI-compatible API through Caddy
- tool execution works in both server and client modes
- the auth DB no longer requires plaintext API-key storage
- metrics are available locally for Prometheus and Grafana
- the stack includes a broader set of utility tools beyond pure chat

## Known Limitations

- Google web-result parsing is not fully reliable, so `web_search` may legitimately fall back to DuckDuckGo.
- `calendar_events` supports ICS data sources, not authenticated personal calendars.
- `shell_safe` is intentionally restricted to an allowlist and should stay that way unless carefully reviewed.
- `sqlite_query` runs inside the adapter container context, so path expectations must match the container filesystem.
- This stack is designed for a trusted local environment, not as a hardened public cloud service.

## Suggested Next Steps

- Add automated adapter unit/integration tests alongside the shell-based `e2e.sh`.
- Consider stronger outbound-network restrictions per tool if security requirements increase.
- Consider authenticated integrations for real calendars or other personal data sources if needed.
- Consider audit logging for tool execution if multiple agents or users will share the stack.
- Consider separate compose profiles for development and production-like operation.

## Agent Onboarding Notes

If you are an agent starting work in this repository, the safest default workflow is:

1. Read [`README.md`](/home/tupolev/llm-stack/README.md) and this document.
2. Inspect [`adapter/app.py`](/home/tupolev/llm-stack/adapter/app.py) before making assumptions.
3. Check whether your task touches runtime code, proxy config, docs, or deployment config.
4. If you modify adapter Python code, rebuild and restart `adapter`.
5. If you modify Caddy routes, reload Caddy.
6. Run [`/home/tupolev/e2e.sh`](/home/tupolev/e2e.sh) when possible.
7. Do not overwrite or rotate `API_KEY_SECRET` or `API_KEY_SALT` casually, or existing API keys will stop working.

This project is intentionally pragmatic: local-first, agent-friendly, and optimized for usefulness over polish.
