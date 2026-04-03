# Product Requirements Document

## Project Name

Local LLM Stack for NUC

## Document Purpose

This document is written for coding agents and advanced maintainers who need to understand, extend, or troubleshoot this project from zero context. It describes the goals, architecture, runtime model, security model, current capabilities, and known limitations of the stack.

Document split:

- [`README.md`](/home/tupolev/README.md) is the operator guide for installation, configuration, and day-to-day operations.
- This PRD is the detailed context document for AI agents and deeper technical onboarding.

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

The main operator is a technically capable user managing the NUC and the Docker stack locally or over SSH.

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

- [`README.md`](/home/tupolev/README.md): operator-facing guide
- [`PRD.md`](/home/tupolev/PRD.md): agent-facing project context
- [`e2e.sh`](/home/tupolev/e2e.sh): black-box end-to-end validation
- [`llm-stack/docker-compose.yml`](/home/tupolev/llm-stack/docker-compose.yml): stack orchestration
- [`llm-stack/Caddyfile`](/home/tupolev/llm-stack/Caddyfile): HTTPS reverse proxy and route mapping
- [`llm-stack/adapter/app.py`](/home/tupolev/llm-stack/adapter/app.py): FastAPI composition, middleware, schedulers, and HTTP routes
- [`llm-stack/adapter/config.py`](/home/tupolev/llm-stack/adapter/config.py): runtime configuration and environment-derived constants
- [`llm-stack/adapter/state.py`](/home/tupolev/llm-stack/adapter/state.py): shared process state, auth DB connection, queues, and metrics
- [`llm-stack/adapter/tooling.py`](/home/tupolev/llm-stack/adapter/tooling.py): tool handlers, schema validation, direct tool execution, and tool registry
- [`llm-stack/adapter/openai_compat.py`](/home/tupolev/llm-stack/adapter/openai_compat.py): OpenAI/Ollama normalization, tool-call extraction, response shaping, and tool orchestration
- [`llm-stack/adapter/auth_security.py`](/home/tupolev/llm-stack/adapter/auth_security.py): API-key hashing, schema helpers, env requirements
- [`llm-stack/adapter/manage_api_keys.py`](/home/tupolev/llm-stack/adapter/manage_api_keys.py): CLI for managing protected API keys
- [`llm-stack/adapter/Dockerfile`](/home/tupolev/llm-stack/adapter/Dockerfile): adapter image build
- [`llm-stack/prometheus.yml`](/home/tupolev/llm-stack/prometheus.yml): Prometheus scrape config
- [`llm-stack/grafana/provisioning/datasources/datasource.yml`](/home/tupolev/llm-stack/grafana/provisioning/datasources/datasource.yml): Grafana datasource config
- [`llm-stack/grafana/provisioning/dashboards/dashboard.yml`](/home/tupolev/llm-stack/grafana/provisioning/dashboards/dashboard.yml): dashboard provisioning
- [`llm-stack/grafana/provisioning/dashboards/llm.json`](/home/tupolev/llm-stack/grafana/provisioning/dashboards/llm.json): dashboard definition

## Adapter Responsibilities

The adapter is the system control plane. It is responsible for:

- OpenAI-compatible chat completions
- embeddings
- model listing
- tool catalog exposure
- tool execution in server mode
- tool-call translation in client mode
- queueing and priority handling
- API key authorization
- metrics

Current internal module split:

- `app.py`: entrypoint and route layer
- `config.py`: configuration constants
- `state.py`: DB connection, queues, counters, and shared metrics
- `tooling.py`: tool handlers and registry
- `openai_compat.py`: OpenAI-compatible request/response translation and tool loop logic

This split is intentional and should be preserved. New changes should usually land in one of these modules rather than growing `app.py` again.

## API Surface

Core endpoints:

- `POST /v1/chat/completions`
- `POST /v1/embeddings`
- `GET /v1/models`
- `GET /v1/tools`
- `GET /v1/openapi.json`
- `GET /metrics`
- `GET /metrics/prometheus`

Direct tool endpoints:

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

### Client Mode

In `tool_execution_mode=client`, the adapter returns OpenAI-style `tool_calls`, allowing the external client or agent runtime to execute tools and feed the result back.

## Authentication and Security Model

API keys must not be stored in plaintext in `auth.db`.

Current implementation:

1. The client sends the API key as a bearer token.
2. The adapter derives `key_hash` from that key using `API_KEY_SECRET` and `API_KEY_SALT`.
3. The adapter looks up `key_hash` in SQLite.
4. If found, the request is authorized and assigned a priority.

Expected schema:

```sql
CREATE TABLE api_keys (
  key_hash TEXT PRIMARY KEY,
  priority TEXT NOT NULL
);
```

Important rule:

- If `API_KEY_SECRET` or `API_KEY_SALT` changes, stored keys will stop matching.

## Configuration Model

The stack uses a local `.env` file loaded by Compose.

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

First run:

```bash
cd /home/tupolev/llm-stack
cp .env.example .env
# edit .env with real values
docker compose up -d
```

If you modify adapter Python code anywhere under [`llm-stack/adapter/`](/home/tupolev/llm-stack/adapter), rebuild and restart the adapter:

```bash
cd /home/tupolev/llm-stack
docker compose build adapter
docker compose up -d adapter
```

If you modify [`Caddyfile`](/home/tupolev/llm-stack/Caddyfile), reload Caddy:

```bash
cd /home/tupolev/llm-stack
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

## End-to-End Validation

The canonical black-box validation script is [`e2e.sh`](/home/tupolev/e2e.sh).

Design requirement:

- it runs from the client machine where an external agent lives
- it must not depend on the NUC’s local `.env` file or shell environment
- it should validate the public API surface only

Current validation coverage:

- `/v1/models`
- streaming chat
- tool calling in server mode
- tool calling in client mode, including round-trip tool result submission
- tool catalog
- direct tool endpoints for weather, time, calendar, fetch URL, news, geocoding, HTTP requests, safe shell, and web search
- `/v1/openapi.json`
- unauthorized access rejection
- Prometheus metrics

Current validation status:

- after the modular refactor of the adapter, `e2e.sh` still passes end-to-end against `https://nuc.fritz.box`
- last validated result: `Summary: total=18 ok=18 fail=0`

## Known Limitations

- Google web-result parsing is not fully reliable, so `web_search` may legitimately fall back to DuckDuckGo.
- `calendar_events` supports ICS data sources, not authenticated personal calendars.
- `shell_safe` is intentionally restricted to an allowlist.
- `sqlite_query` runs inside the adapter container context, so paths must match the container filesystem.
- This stack is designed for a trusted local environment, not as a hardened public cloud service.
- There is currently no per-key rate limiting or anti-starvation scheduling.

## Agent Workflow

If you are an agent starting work in this repository, the safest default workflow is:

1. Read [`README.md`](/home/tupolev/README.md) and this document.
2. Inspect the modular adapter layout, especially [`app.py`](/home/tupolev/llm-stack/adapter/app.py), [`config.py`](/home/tupolev/llm-stack/adapter/config.py), [`state.py`](/home/tupolev/llm-stack/adapter/state.py), [`tooling.py`](/home/tupolev/llm-stack/adapter/tooling.py), and [`openai_compat.py`](/home/tupolev/llm-stack/adapter/openai_compat.py).
3. Check whether your task touches runtime code, proxy config, docs, or deployment config.
4. If you modify adapter Python code, rebuild and restart `adapter`.
5. If you modify Caddy routes, reload Caddy.
6. Run [`e2e.sh`](/home/tupolev/e2e.sh) when possible.
7. Do not rotate `API_KEY_SECRET` or `API_KEY_SALT` casually, or existing API keys will stop working.
