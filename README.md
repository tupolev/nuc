# Local LLM Stack

A local NUC-based LLM stack with:

- Ollama on the host
- a FastAPI adapter exposing an OpenAI-compatible API
- Open WebUI behind Caddy
- Prometheus and Grafana
- SQLite for API keys and priorities
- local HTTPS routing and service orchestration

This README is for the human operator who installs, configures, and runs the project.
If you need deeper technical context for an AI agent, see [`PRD.md`](/home/tupolev/PRD.md).

## What It Does

The stack exposes:

- an OpenAI-style API for clients and coding agents
- a browser UI through Open WebUI
- local JSON and Prometheus metrics
- priority-aware scheduling for chat and embeddings
- OpenAI-style tool calling with client-side execution as the safe default
- workspace-aware file and command tools for agentic coding workflows
- browser-oriented tools for web search and document extraction
- PHP-oriented command support for legacy and modern projects

The adapter is modularized for maintainability:

- [`app.py`](/home/tupolev/llm-stack/adapter/app.py): FastAPI composition and routes
- [`config.py`](/home/tupolev/llm-stack/adapter/config.py): environment variables and constants
- [`state.py`](/home/tupolev/llm-stack/adapter/state.py): auth DB, queues, and metrics
- [`tooling.py`](/home/tupolev/llm-stack/adapter/tooling.py): tools and tool registry
- [`openai_compat.py`](/home/tupolev/llm-stack/adapter/openai_compat.py): OpenAI/Ollama compatibility and tool calling

Agent-facing skills live under [`llm-stack/skills/`](/home/tupolev/llm-stack/skills). These are not Python handlers inside the adapter. They are instruction files intended for coding agents so the agent can follow local conventions when deciding how to use tools. The current example is [`skills/Bash/SKILL.md`](/home/tupolev/llm-stack/skills/Bash/SKILL.md), which tells the agent to use OpenCode's built-in `bash`, `write`, and `edit` tools instead of inventing a fake "Bash" skill or emitting JSON-like tool calls in plain text.

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
  -> /workspace

Prometheus
  -> scrape adapter:4000/metrics/prometheus
  -> scrape ollama-exporter:9101
  -> scrape host.docker.internal:9100

Grafana
  -> reads Prometheus
```

## One-Command Ubuntu Installation

Install everything on a clean Ubuntu machine:

```bash
curl -fsSL https://raw.githubusercontent.com/tupolev/nuc/main/install.sh | bash
```

The installer is intended to:

1. update the system
2. install dependencies such as `git`, `curl`, and `sqlite3`
3. install Docker and Docker Compose
4. clone the project into `/opt/llm-stack`
5. create the auth database and API keys
6. build and start the containers
7. configure systemd services
8. enable autostart
9. warm up Ollama models

## Manual Requirements

If you are not using the installer, you need:

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
- `TOOL_EXECUTION_MODE`
- `AUTO_ENABLE_LOCAL_TOOLS`

Important notes:

- `.env` must not be committed to git
- the `adapter` service loads its configuration through `env_file`
- if you change `API_KEY_SECRET` or `API_KEY_SALT`, existing API keys will stop matching
- the safe default is `TOOL_EXECUTION_MODE=client`
- keep `AUTO_ENABLE_LOCAL_TOOLS=false` unless you intentionally want server-side tool execution

## Tool Execution Safety

The adapter is now biased toward client-side tool execution.

- by default, tools declared by the client are returned to the client for execution
- this is the correct behavior when the coding agent runs on the user's machine
- local server tools are only auto-enabled when a request explicitly asks for `tool_execution_mode=server`
- name collisions are handled safely in `client` mode: if the client exposes a tool called `write_file`, that call is returned to the client instead of being executed on the server
- OpenAI-style compatibility is intentionally broad: the adapter accepts native `tool_calls`, legacy `function_call`, and fallback JSON tool payloads serialized into assistant text
- fallback parsing tolerates common provider quirks such as prose before or after the JSON tool payload
- the adapter also tells models not to invoke missing skills, slash commands, or plugin-style pseudo-tools; they must use only request-scoped tools

This prevents the adapter from writing code files on the NUC when the real workspace lives on the client machine.

Practical expectation for coding agents:

- when the agent runs on the user's machine, file creation and editing must happen there
- tools like `write_file`, `patch_file`, `exec_command`, `create_file`, or equivalent client-defined actions must come back to the client as tool calls
- the NUC should only execute local tools when the caller explicitly opts into server-side execution

About `/workspace`:

- `/workspace` is the adapter's server-side sandbox for local tools, tests, and end-to-end fixtures
- it is still useful for `tool_execution_mode=server` and for validating the adapter itself
- it is not the desired destination for generated project code when OpenCode or another agent is running on the client machine with its own file tools

## Agent Skills

The [`skills/`](/home/tupolev/llm-stack/skills) directory contains skills for the agent, not runtime plugins for the adapter.

- each skill is defined by a `SKILL.md` file with short operational instructions for the agent
- these files are meant to steer agent behavior in client environments such as OpenCode
- they are useful when a model tends to invent pseudo-skills, pseudo-tools, or JSON blobs instead of using the real tools exposed by the client
- the current [`Bash` skill](/home/tupolev/llm-stack/skills/Bash/SKILL.md) exists to reinforce a simple rule: use the built-in `bash`, `write`, and `edit` tools directly

Practical expectation:

- add a skill when the agent needs durable guidance for a recurring workflow
- keep skills small, explicit, and tool-oriented
- do not treat `skills/` as code executed by the adapter; they are instructions consumed by the agent layer

## OpenCode Client Config

A recommended OpenCode client config is available at [`client/opencode/config/opencode.jsonc`](/home/tupolev/llm-stack/client/opencode/config/opencode.jsonc).

This config is intentionally biased toward direct coding tools:

- allows `bash`, `read`, `edit`, `write`, `glob`, and `grep`
- denies `todowrite`, `todoread`, `task`, and `skill`

Why this matters:

- some local models will otherwise burn turns on planning or pseudo-skill flows instead of creating or editing the requested file
- for short coding tasks, denying those meta-tools improves the chance that the model goes straight to `write`/`edit`/`bash`
- if you need a richer planning workflow later, you can relax those denies for stronger models or longer tasks

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
cd /home/tupolev/llm-stack
docker compose ps
```

View adapter logs:

```bash
cd /home/tupolev/llm-stack
docker compose logs -f adapter
```

## systemd Services

If you installed the stack as a system service, useful commands are:

Check main stack service:

```bash
systemctl status llm-stack
```

Check warmup service:

```bash
systemctl status ollama-warmup
```

Restart the stack:

```bash
sudo systemctl restart llm-stack
```

View logs:

```bash
journalctl -u llm-stack -f
journalctl -u ollama-warmup -f
```

Warmup wrapper path:

```text
/usr/local/bin/warm-ollama-wrapper.sh
```

Warmup behavior:

1. waits for Ollama readiness
2. executes `/opt/llm-stack/warm-ollama.sh`

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

## Priority Levels

| Level | Value | Typical use case |
| --- | --- | --- |
| high | 0 | UI or interactive requests |
| medium | 1 | normal usage |
| low | 2 | batch jobs and embeddings |

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

Current configured/tested model names in this stack:

Tool-capable models for agentic clients such as OpenCode:

- `qwen2.5-coder:14b`
- `qwen2.5-coder:7b`
- `qwen3-coder`
- `llama3-groq-tool-use:8b`
- `llama3.1:8b`

Chat-only model in the current Ollama setup:

- `deepseek-coder-v2:16b`

Notes:

- `deepseek-coder-v2:16b` answers normal `/api/chat` requests, but Ollama rejects tool-enabled `/api/chat` requests for this model with `400 Bad Request` and `does not support tools`
- for OpenCode or any other client that must use `bash`, `read`, `write`, `edit`, or similar request-scoped tools, prefer one of the tool-capable models above

Simple chat:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen2.5-coder:14b",
    "messages": [
      {"role":"user","content":"Hello"}
    ]
  }'
```

Priority override:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "X-Priority: high" \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"hello"}]}'
```

Embeddings:

```bash
curl http://localhost:4000/v1/embeddings \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"hello world"}'
```

Streaming chat:

```bash
curl -N http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "llama3.1:8b",
    "stream": true,
    "messages": [
      {"role":"user","content":"Describe this server in one sentence."}
    ]
  }'
```

## Metrics

JSON metrics:

```bash
curl http://localhost:4000/metrics
```

Prometheus metrics:

```bash
curl http://localhost:4000/metrics/prometheus
```

Prometheus scrape target:

```text
http://adapter:4000/metrics/prometheus
```

Grafana:

```text
http://localhost:3001
```

For counters in Grafana, prefer `rate()` or `increase()`.

Example:

```text
rate(llm_requests_total[1m])
```

## End-to-End Validation

The recommended validation script is [`e2e.sh`](/home/tupolev/e2e.sh).

It is meant to run from the client machine where the agent runs, not from the NUC itself, and it only needs:

- `BASE_URL`
- `API_KEY`

Example:

```bash
BASE_URL="https://nuc.fritz.box" API_KEY="YOUR_API_KEY" bash /home/tupolev/e2e.sh -vv
```

Current status:

- `56/56` checks passing

## Current Features

- OpenAI-compatible chat completions
- embeddings
- model listing
- OpenAI-style tools
- compatibility with `tool_calls` and legacy `function_call`
- SSE streaming
- `server` and `client` tool modes
- JSON and Prometheus metrics
- priority scheduling
- separate chat and embeddings queues
- concurrency control
- queue timeout
- local tools for live information and utility actions
- workspace-mounted agentic coding support
- browser extraction tools
- PHP command/runtime support

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
- `list_files`
- `read_file`
- `write_file`
- `patch_file`
- `mkdir`
- `exec_command`
- `browser_search`
- `browser_open`
- `browser_extract`
- `browser_screenshot`

PHP-oriented command support is also available through `exec_command`, including:

- `php`
- `composer`
- `phar`
- `phpunit`
- `phpcs`
- `phpcbf`
- `phpstan`
- `php-cs-fixer`
- `artisan`
- `bin/console`
- `apache2ctl`
- `nginx`

## Scheduler Notes

- non-preemptive
- priority-based queue
- FIFO within the same priority
- chat and embeddings are isolated

## Current Limitations

- no per-key rate limiting
- no anti-starvation mechanism
- single-node Ollama
- not designed as a hardened public internet service

## Quick Troubleshooting

- If you change Python code under [`adapter/`](/home/tupolev/llm-stack/adapter), rebuild and restart `adapter`.
- If you change the proxy config, reload Caddy.
- If an API key stops working, check `API_KEY_SECRET`, `API_KEY_SALT`, and `data/auth.db` first.
- If a tool behaves incorrectly from a client, test the direct endpoint under `/v1/tools/...` first.
- If an agent fails and you are not sure whether the problem is the client or the stack, run `e2e.sh`.
- If a coding agent writes files on the server instead of the client, check that the request is using `client` mode and that server-side tools were not explicitly opted in.
- If a provider emits JSON-like tool payloads inside normal assistant text, the adapter now tries to recover those calls, but native `tool_calls` are still the preferred path.

## Uninstall

```bash
sudo systemctl stop llm-stack
sudo systemctl disable llm-stack
sudo rm -rf /opt/llm-stack
```
