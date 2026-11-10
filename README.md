# Local LLM Stack

A local NUC-based LLM stack with:

- llama.cpp as a Docker Compose service with Vulkan
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
- [`openai_compat.py`](/home/tupolev/llm-stack/adapter/openai_compat.py): OpenAI-compatible backend and tool calling

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
  -> llama-server:8080/v1
  -> /data/auth.db
  -> /data/files
  -> /workspace

Prometheus
  -> scrape adapter:4000/metrics/prometheus
  -> scrape llama-server:8080
  -> scrape host.docker.internal:9100

Grafana
  -> reads Prometheus
```

## One-Command Ubuntu Installation

Install everything on a clean Ubuntu machine:

```bash
curl -fsSL https://raw.githubusercontent.com/tupolev/nuc/main/installer/install.sh | bash
```

The installer is intended to:

1. update the system
2. install dependencies such as `git`, `curl`, and `sqlite3`
3. install Docker and Docker Compose
4. clone the project into `/opt/llm-stack`
5. create the auth database and local `.env`
6. build and start the containers, including llama.cpp
7. configure and enable the stack systemd service

## Manual Requirements

If you are not using the installer, you need:

- Docker and Docker Compose
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

- `LLM_BASE_URL`
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
- `LOCAL_MODEL_FORCE_TOOL_CHOICE=0` is the recommended default. Forcing every request to call a tool can prevent OpenCode from compacting a session.
- `LOCAL_MODEL_FILTER_TOOLS=1` forwards only coding-oriented tools to local models, and only for `opencode` and `coding-agent`
- `LOCAL_MODEL_PSEUDO_TOOL_EXTRACTION=1` allows the adapter to recover a narrow pseudo-call such as `write("file.txt", "content")`, and only for `opencode` and `coding-agent`

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

## Agent Profiles

The adapter now detects a small client profile for each `/v1/chat/completions` request and uses it to decide whether OpenCode-oriented heuristics should run.

Supported profiles:

- `opencode`
- `coding-agent`
- `openwebui`
- `generic`

Alias mapping:

- `codex` -> `coding-agent`
- `claude-code` -> `coding-agent`

Detection order:

1. explicit headers
2. request `metadata`
3. tool-name heuristic
4. fallback to `generic`

Explicit headers recognized by the adapter:

- `X-Agent-Client`
- `X-LLM-Agent`
- `X-Adapter-Agent-Profile`

Supported `metadata` keys:

- `metadata.agent_client`
- `metadata.agent_profile`
- `metadata.client`

OpenCode heuristic detection:

- if the tool list overlaps strongly with OpenCode markers such as `bash`, `read`, `glob`, `grep`, `edit`, `write`, `task`, `webfetch`, `todowrite`, and `skill`, the adapter infers `opencode`

Profile-gated behavior:

- `opencode` and `coding-agent` enable the stronger tool-use system prompt
- `opencode` and `coding-agent` can enable local-tool filtering through `LOCAL_MODEL_FILTER_TOOLS=1`
- `opencode` and `coding-agent` can opt into forced `tool_choice="required"` through `LOCAL_MODEL_FORCE_TOOL_CHOICE=1`; leave it disabled for normal OpenCode sessions.
- `opencode` and `coding-agent` can enable the narrow pseudo-`write(...)` fallback through `LOCAL_MODEL_PSEUDO_TOOL_EXTRACTION=1`
- placeholder path cleanup and malformed schema-description unwrapping are restricted to `opencode` and `coding-agent`

Always-on compatibility behavior for all profiles:

- non-stream tool calls return `content: null` and `finish_reason: "tool_calls"`
- streaming tool calls include the initial `delta.role = "assistant"` chunk
- all chunks in one tool-call stream share the same `chatcmpl-*` id
- all chunks include the requested `model`
- `delta.tool_calls[]` items include `index`
- tool-call streams end with `finish_reason: "tool_calls"` and `data: [DONE]`
- `function.arguments` is always returned as a JSON string

Example explicit profile header for OpenCode-like clients:

```json
{
  "headers": {
    "X-Agent-Client": "opencode"
  }
}
```

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
- if OpenCode can send custom headers, set `X-Agent-Client: opencode` so the adapter can enable the OpenCode profile explicitly instead of relying on tool-name heuristics

### macOS Client

Point OpenCode at the adapter, never at `llama-server` directly. The selectable
model is `nuc/qwen3`, which sends `qwen3:8b` to the NUC.

The NUC certificate is signed by its local mkcert CA. Copy its public root
certificate to the Mac and trust it in the system keychain:

```bash
mkdir -p ~/.config/opencode/certs
scp tupolev@nuc.fritz.box:/home/tupolev/.local/share/mkcert/rootCA.pem \
  ~/.config/opencode/certs/nuc-mkcert-rootCA.pem
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  ~/.config/opencode/certs/nuc-mkcert-rootCA.pem
```

The OpenCode background service needs both the adapter key and the CA path in
its own environment. Do not print or commit the key:

```bash
opencode service set env NUC_LLM_API_KEY "$NUC_LLM_API_KEY"
opencode service set env NODE_EXTRA_CA_CERTS \
  "$HOME/.config/opencode/certs/nuc-mkcert-rootCA.pem"
opencode service restart
```

Configure the custom provider with `https://nuc.fritz.box/v1`, the
`aisdk:@ai-sdk/openai-compatible` package, and a 32K context limit. Keep one
active OpenCode session at a time because the NUC model server exposes one
inference slot.

## Start the Stack

```bash
cd /home/tupolev/llm-stack
docker compose up -d --wait
```

Main services:

- `adapter`
- `open-webui`
- `caddy`
- `llama-server`
- `prometheus`
- `grafana`
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

Check model readiness:

```bash
docker compose ps llama-server
```

Restart the stack:

```bash
sudo systemctl restart llm-stack
```

View logs:

```bash
journalctl -u llm-stack -f
docker compose logs -f llama-server
```

## llama.cpp Runtime

The model server is the `llama-server` Compose service. It uses the official
Vulkan image and downloads `ggml-org/Qwen3-8B-GGUF:Q8_0` with the alias
`qwen3:8b`. Hugging Face data is stored in the managed Docker volume
`hf-cache`.

The server runs one slot with a 32K context window (`--parallel 1` and
`--ctx-size 32768`). This leaves enough room for OpenCode's system prompt and
tool definitions without immediately compacting a new session.

The adapter reaches it over the private Compose network at
`http://llama-server:8080/v1`; clients continue to use the adapter at
`https://nuc.fritz.box/v1`.

Start and inspect it manually:

```bash
cd /home/tupolev/llm-stack
docker compose up -d --wait llama-server
docker compose logs -f llama-server
```

The versioned system service is
[`llm-stack/systemd/llm-stack.service`](/home/tupolev/llm-stack/systemd/llm-stack.service).
It starts Compose with `--wait`, so there is no separate model warmup service.

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

The currently served model is `qwen3:8b` (`ggml-org/Qwen3-8B-GGUF:Q8_0`).
Use the alias returned by `/v1/models` in every client configuration.

Simple chat:

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen3:8b",
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
    "model": "qwen3:8b",
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

Run the validation after changing the adapter, model server, or client
configuration.

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
- one llama.cpp model server and one active inference slot
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
