# OpenCode Setup for This NUC

This guide explains, step by step, how to install OpenCode from scratch and connect it to this NUC's local LLM stack.

It is written for a human user on the client machine where OpenCode will run.

## Goal

At the end of this guide you will be able to:

- install OpenCode
- connect OpenCode to `https://nuc.fritz.box`
- use the NUC adapter as an OpenAI-compatible provider
- ask OpenCode to perform web searches through the adapter tools
- ask OpenCode to inspect and generate source code in this repository

## What You Need

Before starting, make sure you have:

- network access from your client machine to `https://nuc.fritz.box`
- a valid API key created for the adapter
- a local clone of this repository if you want OpenCode to work on the codebase directly

Recommended:

- Windows users: use WSL
- Linux and macOS users: use a modern terminal

## Step 1: Install OpenCode

### Recommended on Windows

OpenCode officially recommends WSL for the best Windows experience.

If WSL is not installed yet, install it first using Microsoft’s guide, then open your WSL terminal.

Install OpenCode in WSL:

```bash
curl -fsSL https://opencode.ai/install | bash
```

### Linux or macOS

Install OpenCode with the official install script:

```bash
curl -fsSL https://opencode.ai/install | bash
```

Alternative install methods also exist, such as:

```bash
npm install -g opencode-ai
```

or on macOS/Linux with Homebrew:

```bash
brew install anomalyco/tap/opencode
```

## Step 2: Confirm the CLI Works

Run:

```bash
opencode --help
```

If that works, the CLI is installed correctly.

## Step 3: Prepare a Project Folder

If you want OpenCode to work on this repository, open the repo root:

```bash
cd /path/to/llm-stack
```

On WSL, that might look like:

```bash
cd /mnt/c/Users/your-user/path/to/llm-stack
```

or, if the repo is already inside Linux:

```bash
cd ~/llm-stack
```

## Step 4: Create an API Key for the NUC Adapter

If you do not already have one, create it on the NUC using the adapter CLI described in the project docs.

The client machine only needs the final API key value.

You will use that key in OpenCode as the credential for the custom provider.

## Step 5: Create a Project OpenCode Config

Create a file named `opencode.json` in the root of the project.

Use this configuration:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "nuc": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "NUC Local LLM Stack",
      "options": {
        "baseURL": "https://nuc.fritz.box/v1",
        "apiKey": "{env:NUC_API_KEY}"
      },
      "models": {
        "qwen2.5-coder:7b": {
          "name": "qwen2.5-coder:7b"
        }
      }
    }
  },
  "model": "nuc/qwen2.5-coder:7b",
  "small_model": "nuc/qwen2.5-coder:7b",
  "instructions": ["./PRD.md"],
  "share": "manual"
}
```

What this does:

- defines a custom provider named `nuc`
- tells OpenCode to use the OpenAI-compatible adapter running at `https://nuc.fritz.box/v1`
- reads the API key from the local environment variable `NUC_API_KEY`
- sets the main model to `qwen2.5-coder:7b`
- loads [`PRD.md`](/home/tupolev/llm-stack/PRD.md) as project instructions/context

## Step 6: Export the API Key on the Client Machine

In the same shell where you will run OpenCode:

```bash
export NUC_API_KEY='YOUR_REAL_API_KEY'
```

If you do not want to export it manually every time, add it to your shell profile, password manager flow, or a local secret-loading script.

Avoid committing secrets into `opencode.json`.

## Step 7: Start OpenCode in the Project

From the project root:

```bash
opencode
```

OpenCode should start in the current directory and load `opencode.json`.

## Step 8: Confirm It Is Using the NUC Model

Inside OpenCode, verify that:

- the active model is `nuc/qwen2.5-coder:7b`
- the session starts without authentication errors

If needed, ask it something simple first:

```text
Describe this repository in one paragraph.
```

If that works, the connection to the NUC is probably correct.

## Step 9: Ask OpenCode to Use Web Search

This project’s adapter exposes a `web_search` tool and related live-data tools.

Use prompts that explicitly tell the model to use the tool. For example:

```text
Use the web_search tool to find the latest OpenAI API models and summarize the top 3 results with links.
```

Another example:

```text
Use web_search and news_search to find recent news about local LLM deployments on NUC or mini PC hardware. Summarize the findings.
```

If the model hesitates, be more explicit:

```text
Do not answer from memory. Use the web_search tool first, then answer.
```

## Step 10: Ask OpenCode to Generate or Modify Code

Once OpenCode is running in the repo, you can ask it to inspect files and write code.

Examples:

```text
Read README.md and PRD.md, then propose a small improvement to adapter/app.py to make weather tool errors easier to debug.
```

```text
Inspect adapter/app.py and add a new tool endpoint for exchange rates. Update README.md and PRD.md too.
```

```text
Search the repo for auth.db usage and explain the current API key verification flow.
```

```text
Generate a new Python module for validating HTTP tool arguments and integrate it into adapter/app.py.
```

## Step 11: Good Prompting Patterns for This Project

For tool use:

```text
Use the available tools instead of guessing. If fresh information is needed, use web_search, news_search, fetch_url, weather, time, or http_request.
```

For code changes:

```text
Inspect the repository first, then implement the change. If you modify adapter Python code, remind me that adapter must be rebuilt and restarted.
```

For safe debugging:

```text
Check whether the issue is in the client, Caddy, or adapter. Prefer verifying with direct API calls before proposing a fix.
```

## Optional: Use OpenCode's Interactive Credential Flow

OpenCode also supports adding credentials interactively through its provider connection flow.

For a custom OpenAI-compatible provider, the documented pattern is:

1. Run the provider connection flow.
2. Choose `Other`.
3. Enter a provider ID such as `nuc`.
4. Enter the API key.
5. Still create `opencode.json` so OpenCode knows the custom provider base URL and model list.

For this project, the environment-variable method shown earlier is usually simpler and easier to reproduce.

## Troubleshooting

### `401 Invalid API key`

Check:

- `NUC_API_KEY` is set in the current shell
- the key is valid in the NUC adapter
- the adapter is reachable at `https://nuc.fritz.box/v1`

You can test the API outside OpenCode:

```bash
curl -k https://nuc.fritz.box/v1/models \
  -H "Authorization: Bearer $NUC_API_KEY"
```

### OpenCode starts but does not use the NUC provider

Check:

- you launched OpenCode from the directory containing `opencode.json`
- the `model` field is `nuc/qwen2.5-coder:7b`
- the `provider.nuc` block exists and is valid JSON

### Tool use feels weak or inconsistent

This usually means one of these:

- the model was not explicitly told to use a tool
- the task was ambiguous
- the model answered from prior knowledge instead of calling the tool

Prompt more explicitly:

```text
Use web_search first. Do not answer from memory.
```

### Web search returns mixed quality results

That is expected sometimes. The adapter tries Google first in auto mode and may fall back to DuckDuckGo if Google parsing is not stable enough.

### Code edits are not being applied as expected

Make sure:

- you started OpenCode in the repository root
- the files are writable
- the prompt clearly says whether you want analysis only or real code changes

## Recommended Workflow for This Repository

1. Start OpenCode in the repo root.
2. Make sure `PRD.md` is available and referenced in `opencode.json`.
3. Ask OpenCode to inspect before editing.
4. After Python changes under `adapter/`, rebuild the adapter:

```bash
docker compose build adapter
docker compose up -d adapter
```

5. If proxy routes changed, reload Caddy:

```bash
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

6. Validate with:

```bash
BASE_URL="https://nuc.fritz.box" API_KEY="$NUC_API_KEY" bash /home/tupolev/e2e.sh -vv
```

## Minimal Quick Start

If you only want the shortest working path:

1. Install OpenCode:

```bash
curl -fsSL https://opencode.ai/install | bash
```

2. Go to the repo:

```bash
cd /path/to/llm-stack
```

3. Create `opencode.json` with the config shown above.

4. Export the key:

```bash
export NUC_API_KEY='YOUR_REAL_API_KEY'
```

5. Start OpenCode:

```bash
opencode
```

6. Ask:

```text
Use web_search to find the latest information about OpenAI-compatible coding agents, then summarize the results.
```

7. Ask:

```text
Inspect adapter/app.py and generate a patch to improve error handling in one tool.
```
