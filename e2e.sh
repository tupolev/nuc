#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:4000}"
API_KEY="${API_KEY:-}"
MODEL="${MODEL:-qwen2.5-coder:7b}"
VERBOSE=0
TEST_WORKSPACE_DIR="${TEST_WORKSPACE_DIR:-test-app}"
STACK_ENV_FILE="${STACK_ENV_FILE:-/home/tupolev/llm-stack/.env}"

usage() {
  cat <<'USAGE'
Usage:
  BASE_URL="http://127.0.0.1:4000" API_KEY="<key>" bash e2e.sh [-vv]

Options:
  -vv   Verbose mode: show request payloads and full outputs.
USAGE
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "-vv" ]]; then
  VERBOSE=1
  shift
fi

if [[ $# -gt 0 ]]; then
  echo "Unknown argument: $1"
  usage
  exit 1
fi

if [[ -z "$API_KEY" && -f "$STACK_ENV_FILE" ]]; then
  API_KEY=$(awk -F= '/^OPENWEBUI_ADAPTER_API_KEY=/{print substr($0, index($0, "=") + 1)}' "$STACK_ENV_FILE" | tail -n1)
fi

if [[ -z "$API_KEY" ]]; then
  echo "Error: API_KEY is not set and no OPENWEBUI_ADAPTER_API_KEY was found in $STACK_ENV_FILE"
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is not installed. Install it with: sudo apt-get install -y jq"
  exit 1
fi

if command -v rg >/dev/null 2>&1; then
  FILTER_CMD=(rg)
else
  FILTER_CMD=(grep -E)
fi

TOTAL=0
PASSED=0
FAILED=0
left='$left'
right='$right'
calculator='$calculator'
finder='$finder'

pass() {
  local name="$1"
  echo "[$TOTAL] OK   $name"
  PASSED=$((PASSED + 1))
}

fail() {
  local name="$1"
  local reason="$2"
  echo "[$TOTAL] FAIL $name"
  echo "      reason: $reason"
  FAILED=$((FAILED + 1))
}

run_json_test() {
  local name="$1"
  local endpoint="$2"
  local payload="$3"
  local jq_expr="$4"

  TOTAL=$((TOTAL + 1))

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- REQUEST: $name ---"
    echo "$payload" | jq . || echo "$payload"
  fi

  local raw status body
  raw=$(curl -k -sS "$BASE_URL$endpoint" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    -w '\nHTTP_STATUS:%{http_code}\n') || {
      fail "$name" "network/curl error"
      return
    }

  status=$(echo "$raw" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  body=$(echo "$raw" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo "--- RESPONSE: $name (HTTP $status) ---"
    echo "$body" | jq . 2>/dev/null || echo "$body"
  fi

  if [[ "$status" != "200" ]]; then
    fail "$name" "HTTP $status: $(echo "$body" | tr '\n' ' ' | cut -c1-240)"
    return
  fi

  if echo "$body" | jq -e "$jq_expr" >/dev/null 2>&1; then
    pass "$name"
  else
    fail "$name" "JSON validation failed for jq expression: $jq_expr"
  fi
}

run_get_test() {
  local name="$1"
  local endpoint="$2"
  local jq_expr="$3"

  TOTAL=$((TOTAL + 1))

  local raw status body
  raw=$(curl -k -sS "$BASE_URL$endpoint" \
    -H "Authorization: Bearer $API_KEY" \
    -w '\nHTTP_STATUS:%{http_code}\n') || {
      fail "$name" "network/curl error"
      return
    }

  status=$(echo "$raw" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  body=$(echo "$raw" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- RESPONSE: $name (HTTP $status) ---"
    echo "$body" | jq . 2>/dev/null || echo "$body"
  fi

  if [[ "$status" != "200" ]]; then
    fail "$name" "HTTP $status"
    return
  fi

  if echo "$body" | jq -e "$jq_expr" >/dev/null 2>&1; then
    pass "$name"
  else
    fail "$name" "JSON validation failed for jq expression: $jq_expr"
  fi
}

run_status_test() {
  local name="$1"
  local endpoint="$2"
  local expected_status="$3"
  local auth_header="${4:-Bearer $API_KEY}"

  TOTAL=$((TOTAL + 1))

  local raw status body
  raw=$(curl -k -sS "$BASE_URL$endpoint" \
    -H "Authorization: $auth_header" \
    -w '\nHTTP_STATUS:%{http_code}\n') || {
      fail "$name" "network/curl error"
      return
    }

  status=$(echo "$raw" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  body=$(echo "$raw" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- RESPONSE: $name (HTTP $status) ---"
    echo "$body" | jq . 2>/dev/null || echo "$body"
  fi

  if [[ "$status" == "$expected_status" ]]; then
    pass "$name"
  else
    fail "$name" "expected HTTP $expected_status, got HTTP $status"
  fi
}

WORKSPACE_PREFIX="$TEST_WORKSPACE_DIR"
PHP_WORKSPACE_PREFIX="${PHP_WORKSPACE_PREFIX:-test-app-php}"

PHP_SOURCE_CONTENT=$(cat <<'EOF'
<?php

declare(strict_types=1);

namespace Nuc\TestApp;

final class Calculator
{
    public function add(int $left, int $right): int
    {
        return $left + $right;
    }
}
EOF
)

PHP_TEST_CONTENT=$(cat <<'EOF'
<?php

declare(strict_types=1);

namespace Nuc\TestApp\Tests;

use Nuc\TestApp\Calculator;
use PHPUnit\Framework\TestCase;

final class CalculatorTest extends TestCase
{
    public function testAddReturnsTheSum(): void
    {
        $calculator = new Calculator();

        self::assertSame(5, $calculator->add(2, 3));
    }
}
EOF
)

PHP_FIXER_CONTENT=$(cat <<'EOF'
<?php

declare(strict_types=1);

$finder = PhpCsFixer\Finder::create()
    ->in(__DIR__ . '/src')
    ->in(__DIR__ . '/tests');

return (new PhpCsFixer\Config())
    ->setRiskyAllowed(true)
    ->setRules([
        '@PSR12' => true,
    ])
    ->setFinder($finder);
EOF
)

# 1) Smoke models
run_get_test \
  "Smoke /v1/models" \
  "/v1/models" \
  '.object == "list" and (.data | type == "array") and (.data | length > 0)'

# 2) Streaming chat without tools
TOTAL=$((TOTAL + 1))
STREAM_PAYLOAD=$(cat <<JSON
{
  "model": "$MODEL",
  "stream": true,
  "messages": [{"role":"user","content":"Describe this server in one sentence."}]
}
JSON
)

if [[ $VERBOSE -eq 1 ]]; then
  echo
  echo "--- REQUEST: Streaming chat ---"
  echo "$STREAM_PAYLOAD" | jq .
fi

stream_out=$(curl -k -sS -N --max-time 90 "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$STREAM_PAYLOAD") || {
    fail "Streaming chat" "network/curl error or timeout"
    stream_out=""
}

if [[ -n "${stream_out:-}" ]]; then
  if [[ $VERBOSE -eq 1 ]]; then
    echo "--- RESPONSE: Streaming chat ---"
    echo "$stream_out"
  fi

  if echo "$stream_out" | "${FILTER_CMD[@]}" "\[DONE\]" >/dev/null 2>&1; then
    pass "Streaming chat"
  else
    fail "Streaming chat" "SSE stream did not include [DONE]"
  fi
fi

# 3) Tools in server mode
PAYLOAD_SERVER=$(cat <<JSON
{
  "model": "$MODEL",
  "tool_execution_mode": "server",
  "tool_choice": "required",
  "tools": [
    {"type":"function","function":{"name":"python","description":"Run Python","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}}
  ],
  "messages": [{"role":"user","content":"Compute 19*23 using python and reply with only the number."}]
}
JSON
)

run_json_test \
  "Tools server mode" \
  "/v1/chat/completions" \
  "$PAYLOAD_SERVER" \
  '.choices[0].finish_reason == "stop" and (.choices[0].message.content | type == "string")'

# 4) Tools in client mode with external passthrough (turn 1)
TOTAL=$((TOTAL + 1))
PAYLOAD_CLIENT=$(cat <<JSON
{
  "model": "$MODEL",
  "tool_execution_mode": "client",
  "tools": [
    {"type":"function","function":{"name":"external_math","description":"Compute a product","parameters":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},"required":["a","b"]}}}
  ],
  "messages": [{"role":"user","content":"Use the external_math tool to compute 19*23."}]
}
JSON
)

if [[ $VERBOSE -eq 1 ]]; then
  echo
  echo "--- REQUEST: Tools client mode (turn 1) ---"
  echo "$PAYLOAD_CLIENT" | jq .
fi

raw_client=$(curl -k -sS "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD_CLIENT" \
  -w '\nHTTP_STATUS:%{http_code}\n') || {
    fail "Tools client mode (turn 1)" "network/curl error"
    raw_client=""
}

CALL_ID=""
CALL_OBJ=""

if [[ -n "${raw_client:-}" ]]; then
  status=$(echo "$raw_client" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  client_body=$(echo "$raw_client" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo "--- RESPONSE: Tools client mode (turn 1) (HTTP $status) ---"
    echo "$client_body" | jq . 2>/dev/null || echo "$client_body"
  fi

  if [[ "$status" != "200" ]]; then
    fail "Tools client mode (turn 1)" "HTTP $status"
  elif echo "$client_body" | jq -e '.choices[0].finish_reason == "tool_calls" and (.choices[0].message.tool_calls | type == "array") and (.choices[0].message.tool_calls | length > 0) and (.choices[0].message.tool_calls[0].function.name == "external_math")' >/dev/null 2>&1; then
    CALL_ID=$(echo "$client_body" | jq -r '.choices[0].message.tool_calls[0].id')
    CALL_OBJ=$(echo "$client_body" | jq -c '.choices[0].message.tool_calls[0]')
    pass "Tools client mode (turn 1)"
  else
    fail "Tools client mode (turn 1)" "no valid tool_calls returned"
  fi
fi

# 5) Tools in client mode (manual turn 2)
TOTAL=$((TOTAL + 1))
if [[ -z "$CALL_ID" || -z "$CALL_OBJ" ]]; then
  fail "Tools client mode (turn 2)" "skipped: no valid prior tool_call"
else
  PAYLOAD_CLIENT_2=$(cat <<JSON
{
  "model": "$MODEL",
  "tool_execution_mode": "client",
  "tools": [
    {"type":"function","function":{"name":"external_math","description":"Compute a product","parameters":{"type":"object","properties":{"a":{"type":"integer"},"b":{"type":"integer"}},"required":["a","b"]}}}
  ],
  "messages": [
    {"role":"user","content":"Use the external_math tool to compute 19*23."},
    {"role":"assistant","content":null,"tool_calls":[${CALL_OBJ}]},
    {"role":"tool","tool_call_id":"${CALL_ID}","name":"external_math","content":"{\"result\":437}"}
  ]
}
JSON
)

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- REQUEST: Tools client mode (turn 2) ---"
    echo "$PAYLOAD_CLIENT_2" | jq .
  fi

  raw_client2=$(curl -k -sS "$BASE_URL/v1/chat/completions" \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD_CLIENT_2" \
    -w '\nHTTP_STATUS:%{http_code}\n') || {
      fail "Tools client mode (turn 2)" "network/curl error"
      raw_client2=""
    }

  if [[ -n "${raw_client2:-}" ]]; then
    status=$(echo "$raw_client2" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
    client2_body=$(echo "$raw_client2" | sed '/^HTTP_STATUS:/d')

    if [[ $VERBOSE -eq 1 ]]; then
      echo "--- RESPONSE: Tools client mode (turn 2) (HTTP $status) ---"
      echo "$client2_body" | jq . 2>/dev/null || echo "$client2_body"
    fi

    if [[ "$status" != "200" ]]; then
      fail "Tools client mode (turn 2)" "HTTP $status"
    elif echo "$client2_body" | jq -e '.choices[0].finish_reason == "stop" and (.choices[0].message.content | type == "string")' >/dev/null 2>&1; then
      pass "Tools client mode (turn 2)"
    else
      fail "Tools client mode (turn 2)" "invalid final response"
    fi
  fi
fi

# 6) Tools client mode with colliding write_file passthrough
TOTAL=$((TOTAL + 1))
PAYLOAD_CLIENT_COLLISION=$(cat <<JSON
{
  "model": "$MODEL",
  "tool_execution_mode": "client",
  "tool_choice": "required",
  "tools": [
    {"type":"function","function":{"name":"write_file","description":"Client-side file writer","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}}}
  ],
  "messages": [{"role":"user","content":"Write hello.txt with hello from the client tool."}]
}
JSON
)

raw_collision=$(curl -k -sS "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD_CLIENT_COLLISION" \
  -w '\nHTTP_STATUS:%{http_code}\n') || {
    fail "Tools client mode collision passthrough" "network/curl error"
    raw_collision=""
  }

if [[ -n "${raw_collision:-}" ]]; then
  status=$(echo "$raw_collision" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  collision_body=$(echo "$raw_collision" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- RESPONSE: Tools client mode collision passthrough (HTTP $status) ---"
    echo "$collision_body" | jq . 2>/dev/null || echo "$collision_body"
  fi

  if [[ "$status" != "200" ]]; then
    fail "Tools client mode collision passthrough" "HTTP $status"
  elif echo "$collision_body" | jq -e '.choices[0].finish_reason == "tool_calls" and (.choices[0].message.tool_calls[0].function.name == "write_file")' >/dev/null 2>&1; then
    pass "Tools client mode collision passthrough"
  else
    fail "Tools client mode collision passthrough" "adapter did not return client-side write_file tool call"
  fi
fi

# 7) Fallback parser accepts JSON tool payload followed by prose
TOTAL=$((TOTAL + 1))
fallback_parser_output=$(docker compose -f /home/tupolev/llm-stack/docker-compose.yml exec -T adapter python - <<'PY'
from openai_compat import extract_tool_calls_from_content

tools = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Client-side file creator",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]

content = """{"name": "create_file", "arguments": {"file_path": "./weather", "content": "#!/bin/bash\\nexit 0\\n"}} Confirmation that the file was created: ./weather Usage example: ./weather Berlin"""
print(extract_tool_calls_from_content(content, tools))
PY
) || fallback_parser_output=""

if echo "$fallback_parser_output" | grep -q "create_file"; then
  pass "Fallback parser handles JSON plus prose"
else
  fail "Fallback parser handles JSON plus prose" "parser did not recover tool call from mixed content"
fi

# 8) Fallback parser accepts prose before JSON tool payload
TOTAL=$((TOTAL + 1))
fallback_prefixed_output=$(docker compose -f /home/tupolev/llm-stack/docker-compose.yml exec -T adapter python - <<'PY'
from openai_compat import extract_tool_calls_from_content

tools = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Client-side file writer",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]

content = """I'll create the file now.\n{"name": "write_file", "arguments": {"path": "./hello.txt", "content": "hello"}}"""
print(extract_tool_calls_from_content(content, tools))
PY
) || fallback_prefixed_output=""

if echo "$fallback_prefixed_output" | grep -q "write_file"; then
  pass "Fallback parser handles prose before JSON"
else
  fail "Fallback parser handles prose before JSON" "parser did not recover prefixed tool call"
fi

# 9) Legacy function_call is accepted
TOTAL=$((TOTAL + 1))
legacy_function_call_output=$(docker compose -f /home/tupolev/llm-stack/docker-compose.yml exec -T adapter python - <<'PY'
from openai_compat import extract_tool_calls

tools = [
    {
        "type": "function",
        "function": {
            "name": "create_file",
            "description": "Client-side file creator",
            "parameters": {"type": "object", "properties": {}},
        },
    }
]

raw_message = {
    "function_call": {
        "name": "create_file",
        "arguments": {"file_path": "./weather", "content": "#!/bin/bash\nexit 0\n"},
    }
}
print(extract_tool_calls(raw_message, tools))
PY
) || legacy_function_call_output=""

if echo "$legacy_function_call_output" | grep -q "create_file"; then
  pass "Legacy function_call compatibility"
else
  fail "Legacy function_call compatibility" "legacy function_call was not normalized"
fi

# 10) Tools catalog
run_get_test \
  "Tools catalog" \
  "/v1/tools" \
  '.data | type == "array"
    and (map(.function.name) | index("python")) != null
    and (map(.function.name) | index("weather")) != null
    and (map(.function.name) | index("news_search")) != null
    and (map(.function.name) | index("browser_search")) != null
    and (map(.function.name) | index("browser_open")) != null
    and (map(.function.name) | index("browser_extract")) != null
    and (map(.function.name) | index("list_files")) != null
    and (map(.function.name) | index("read_file")) != null
    and (map(.function.name) | index("write_file")) != null
    and (map(.function.name) | index("patch_file")) != null
    and (map(.function.name) | index("mkdir")) != null
    and (map(.function.name) | index("exec_command")) != null'

# 11) Workspace mkdir
run_json_test \
  "Workspace mkdir" \
  "/v1/tools/mkdir" \
  "{\"path\":\"${WORKSPACE_PREFIX}\",\"parents\":true}" \
  '.path == "'"${WORKSPACE_PREFIX}"'" and (.created | type == "boolean")'

# 12) Workspace write file
run_json_test \
  "Workspace write file" \
  "/v1/tools/write_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/notes.txt\",\"content\":\"hello\\nworld\\n\",\"create_dirs\":true}" \
  ".path == \"${WORKSPACE_PREFIX}/notes.txt\" and .bytes_written > 0"

# 13) Workspace read file
run_json_test \
  "Workspace read file" \
  "/v1/tools/read_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/notes.txt\"}" \
  ".path == \"${WORKSPACE_PREFIX}/notes.txt\" and (.content | contains(\"hello\"))"

# 14) Workspace patch file
run_json_test \
  "Workspace patch file" \
  "/v1/tools/patch_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/notes.txt\",\"old_text\":\"world\",\"new_text\":\"agent\",\"replace_all\":false}" \
  ".path == \"${WORKSPACE_PREFIX}/notes.txt\" and .replacements == 1"

# 12) Workspace list files
run_json_test \
  "Workspace list files" \
  "/v1/tools/list_files" \
  "{\"path\":\"${WORKSPACE_PREFIX}\",\"recursive\":true,\"max_entries\":20}" \
  ".path == \"${WORKSPACE_PREFIX}\" and (.entries | type == \"array\") and ((.entries | map(.path) | index(\"${WORKSPACE_PREFIX}/notes.txt\")) != null)"

# 13) Workspace exec command
run_json_test \
  "Workspace exec command" \
  "/v1/tools/exec_command" \
  "{\"command\":\"python3 -c \\\"print(437)\\\"\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":10}" \
  '.returncode == 0 and (.stdout | contains("437")) and .timed_out == false'

# 14) Workspace invalid path
run_json_test \
  "Workspace invalid path" \
  "/v1/tools/read_file" \
  '{"path":"../../etc/passwd"}' \
  '.error == "path escapes the workspace root"'

# 15) Exec command blocked executable
run_json_test \
  "Exec command blocked executable" \
  "/v1/tools/exec_command" \
  '{"command":"ls","cwd":".","timeout_seconds":5}' \
  '(.error | contains("EXEC_COMMAND_ALLOWLIST"))'

# 16) Exec command timeout
run_json_test \
  "Exec command timeout" \
  "/v1/tools/exec_command" \
  '{"command":"python3 -c \"import time; time.sleep(2)\"","cwd":".","timeout_seconds":1}' \
  '.timed_out == true and .timeout_seconds == 1'

# 17) App scaffold package.json
run_json_test \
  "App scaffold package.json" \
  "/v1/tools/write_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/package.json\",\"content\":\"{\\n  \\\"name\\\": \\\"test-app\\\",\\n  \\\"private\\\": true,\\n  \\\"version\\\": \\\"0.0.0\\\",\\n  \\\"type\\\": \\\"module\\\",\\n  \\\"scripts\\\": {\\n    \\\"build\\\": \\\"vite build\\\"\\n  },\\n  \\\"devDependencies\\\": {\\n    \\\"vite\\\": \\\"^7.1.7\\\"\\n  }\\n}\\n\",\"create_dirs\":true}" \
  ".path == \"${WORKSPACE_PREFIX}/package.json\" and .bytes_written > 0"

# 18) App scaffold index.html
run_json_test \
  "App scaffold index.html" \
  "/v1/tools/write_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/index.html\",\"content\":\"<!doctype html>\\n<html lang=\\\"en\\\">\\n  <head>\\n    <meta charset=\\\"UTF-8\\\" />\\n    <meta name=\\\"viewport\\\" content=\\\"width=device-width, initial-scale=1.0\\\" />\\n    <title>NUC Test App</title>\\n  </head>\\n  <body>\\n    <div id=\\\"app\\\"></div>\\n    <script type=\\\"module\\\" src=\\\"/src/main.js\\\"></script>\\n  </body>\\n</html>\\n\",\"create_dirs\":true}" \
  ".path == \"${WORKSPACE_PREFIX}/index.html\" and .bytes_written > 0"

# 19) App scaffold entrypoint
run_json_test \
  "App scaffold entrypoint" \
  "/v1/tools/write_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/src/main.js\",\"content\":\"document.querySelector(\\\"#app\\\").innerHTML = '<main><h1>NUC test app</h1><p>If you can read this, file tools and build tooling work.</p></main>';\\n\",\"create_dirs\":true}" \
  ".path == \"${WORKSPACE_PREFIX}/src/main.js\" and .bytes_written > 0"

# 20) App install deps
run_json_test \
  "App install deps" \
  "/v1/tools/exec_command" \
  "{\"command\":\"npm install\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":300}" \
  '.returncode == 0 and .timed_out == false'

# 21) App build
run_json_test \
  "App build" \
  "/v1/tools/exec_command" \
  "{\"command\":\"npm run build\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":300}" \
  '.returncode == 0 and (.stdout | contains("vite build")) and (.stdout | contains("built in")) and .timed_out == false'

# 22) App dist output
run_json_test \
  "App dist output" \
  "/v1/tools/list_files" \
  "{\"path\":\"${WORKSPACE_PREFIX}/dist\",\"recursive\":true,\"max_entries\":50}" \
  ".path == \"${WORKSPACE_PREFIX}/dist\" and ((.entries | map(.path) | index(\"${WORKSPACE_PREFIX}/dist/index.html\")) != null)"

# 22) PHP runtime
run_json_test \
  "PHP runtime" \
  "/v1/tools/exec_command" \
  "{\"command\":\"php -v\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and (.stdout | contains("PHP ")) and .timed_out == false'

# 23) Composer runtime
run_json_test \
  "Composer runtime" \
  "/v1/tools/exec_command" \
  "{\"command\":\"composer --version\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and (.stdout | contains("Composer version")) and .timed_out == false'

# 24) PHPUnit runtime
run_json_test \
  "PHPUnit runtime" \
  "/v1/tools/exec_command" \
  "{\"command\":\"phpunit --version\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and (.stdout | contains("PHPUnit")) and .timed_out == false'

# 25) Apache runtime
run_json_test \
  "Apache runtime" \
  "/v1/tools/exec_command" \
  "{\"command\":\"apache2ctl -v\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and (.stdout | contains("Apache/")) and .timed_out == false'

# 26) Nginx runtime
run_json_test \
  "Nginx runtime" \
  "/v1/tools/exec_command" \
  "{\"command\":\"nginx -v\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and (.stderr | contains("nginx version")) and .timed_out == false'

# 27) Framework console scaffold
run_json_test \
  "Framework console scaffold" \
  "/v1/tools/write_file" \
  "{\"path\":\"${WORKSPACE_PREFIX}/bin/console\",\"content\":\"#!/usr/bin/env php\\n<?php\\necho \\\"Symfony Console 0.1\\\\n\\\";\\n\",\"create_dirs\":true}" \
  ".path == \"${WORKSPACE_PREFIX}/bin/console\" and .bytes_written > 0"

# 28) Framework console chmod
run_json_test \
  "Framework console chmod" \
  "/v1/tools/exec_command" \
  "{\"command\":\"php -r \\\"chmod('bin/console', 0755);\\\"\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and .timed_out == false'

# 29) Framework console execute
run_json_test \
  "Framework console execute" \
  "/v1/tools/exec_command" \
  "{\"command\":\"bin/console --version\",\"cwd\":\"${WORKSPACE_PREFIX}\",\"timeout_seconds\":30}" \
  '.returncode == 0 and (.stdout | contains("Symfony Console 0.1")) and .timed_out == false'

# 30) PHP project composer.json
run_json_test \
  "PHP project composer.json" \
  "/v1/tools/write_file" \
  "{\"path\":\"${PHP_WORKSPACE_PREFIX}/composer.json\",\"content\":\"{\\n  \\\"name\\\": \\\"nuc/test-app-php\\\",\\n  \\\"type\\\": \\\"project\\\",\\n  \\\"require\\\": {},\\n  \\\"require-dev\\\": {\\n    \\\"friendsofphp/php-cs-fixer\\\": \\\"^3.66\\\",\\n    \\\"phpstan/phpstan\\\": \\\"^2.1\\\",\\n    \\\"phpunit/phpunit\\\": \\\"^11.5\\\"\\n  },\\n  \\\"autoload\\\": {\\n    \\\"psr-4\\\": {\\n      \\\"Nuc\\\\\\\\TestApp\\\\\\\\\\\": \\\"src/\\\"\\n    }\\n  },\\n  \\\"autoload-dev\\\": {\\n    \\\"psr-4\\\": {\\n      \\\"Nuc\\\\\\\\TestApp\\\\\\\\Tests\\\\\\\\\\\": \\\"tests/\\\"\\n    }\\n  },\\n  \\\"scripts\\\": {\\n    \\\"test\\\": \\\"vendor/bin/phpunit --colors=never\\\",\\n    \\\"analyse\\\": \\\"vendor/bin/phpstan analyse src tests --no-progress\\\",\\n    \\\"format-check\\\": \\\"vendor/bin/php-cs-fixer fix --dry-run --diff --using-cache=no\\\"\\n  }\\n}\\n\",\"create_dirs\":true}" \
  ".path == \"${PHP_WORKSPACE_PREFIX}/composer.json\" and .bytes_written > 0"

# 31) PHP project source
run_json_test \
  "PHP project source" \
  "/v1/tools/write_file" \
  "$(jq -n --arg path "${PHP_WORKSPACE_PREFIX}/src/Calculator.php" --arg content "$PHP_SOURCE_CONTENT" '{path:$path,content:$content,create_dirs:true}')" \
  ".path == \"${PHP_WORKSPACE_PREFIX}/src/Calculator.php\" and .bytes_written > 0"

# 32) PHP project test
run_json_test \
  "PHP project test" \
  "/v1/tools/write_file" \
  "$(jq -n --arg path "${PHP_WORKSPACE_PREFIX}/tests/CalculatorTest.php" --arg content "$PHP_TEST_CONTENT" '{path:$path,content:$content,create_dirs:true}')" \
  ".path == \"${PHP_WORKSPACE_PREFIX}/tests/CalculatorTest.php\" and .bytes_written > 0"

# 33) PHP project phpunit config
run_json_test \
  "PHP project phpunit config" \
  "/v1/tools/write_file" \
  "{\"path\":\"${PHP_WORKSPACE_PREFIX}/phpunit.xml\",\"content\":\"<?xml version=\\\"1.0\\\" encoding=\\\"UTF-8\\\"?>\\n<phpunit bootstrap=\\\"vendor/autoload.php\\\" cacheDirectory=\\\".phpunit.cache\\\" colors=\\\"false\\\">\\n  <testsuites>\\n    <testsuite name=\\\"default\\\">\\n      <directory>tests</directory>\\n    </testsuite>\\n  </testsuites>\\n</phpunit>\\n\",\"create_dirs\":true}" \
  ".path == \"${PHP_WORKSPACE_PREFIX}/phpunit.xml\" and .bytes_written > 0"

# 34) PHP project phpstan config
run_json_test \
  "PHP project phpstan config" \
  "/v1/tools/write_file" \
  "{\"path\":\"${PHP_WORKSPACE_PREFIX}/phpstan.neon\",\"content\":\"parameters:\\n  level: 1\\n  paths:\\n    - src\\n    - tests\\n\",\"create_dirs\":true}" \
  ".path == \"${PHP_WORKSPACE_PREFIX}/phpstan.neon\" and .bytes_written > 0"

# 35) PHP project fixer config
run_json_test \
  "PHP project fixer config" \
  "/v1/tools/write_file" \
  "$(jq -n --arg path "${PHP_WORKSPACE_PREFIX}/.php-cs-fixer.dist.php" --arg content "$PHP_FIXER_CONTENT" '{path:$path,content:$content,create_dirs:true}')" \
  ".path == \"${PHP_WORKSPACE_PREFIX}/.php-cs-fixer.dist.php\" and .bytes_written > 0"

# 36) PHP project composer install
run_json_test \
  "PHP project composer install" \
  "/v1/tools/exec_command" \
  "{\"command\":\"composer install --no-interaction --no-progress\",\"cwd\":\"${PHP_WORKSPACE_PREFIX}\",\"timeout_seconds\":600}" \
  '.returncode == 0 and ((.stdout | contains("Generating autoload files")) or (.stdout | contains("Installing dependencies from lock file")) or (.stderr | contains("Generating autoload files"))) and .timed_out == false'

# 37) PHP project phpunit
run_json_test \
  "PHP project phpunit" \
  "/v1/tools/exec_command" \
  "{\"command\":\"vendor/bin/phpunit --colors=never\",\"cwd\":\"${PHP_WORKSPACE_PREFIX}\",\"timeout_seconds\":120}" \
  '.returncode == 0 and (((.stdout | contains("OK")) or (.stderr | contains("OK"))) or ((.stdout | contains("PHPUnit")) or (.stderr | contains("PHPUnit")))) and .timed_out == false'

# 38) PHP project phpstan
run_json_test \
  "PHP project phpstan" \
  "/v1/tools/exec_command" \
  "{\"command\":\"vendor/bin/phpstan analyse src tests --no-progress\",\"cwd\":\"${PHP_WORKSPACE_PREFIX}\",\"timeout_seconds\":120}" \
  '.returncode == 0 and ((.stdout | contains("[OK] No errors")) or (.stdout | contains("No errors")) or (.stderr | contains("No errors"))) and .timed_out == false'

# 39) PHP project fixer
run_json_test \
  "PHP project fixer" \
  "/v1/tools/exec_command" \
  "{\"command\":\"vendor/bin/php-cs-fixer fix --dry-run --diff --using-cache=no\",\"cwd\":\"${PHP_WORKSPACE_PREFIX}\",\"timeout_seconds\":120}" \
  '(.returncode == 0 or .returncode == 8) and (((.stdout | contains("Found")) or (.stdout | contains("fixed"))) or ((.stderr | contains("PHP CS Fixer")) or (.stderr | contains("Loaded config")))) and .timed_out == false'

# 40) Agentic prompt build
TOTAL=$((TOTAL + 1))
AGENTIC_PAYLOAD=$(cat <<JSON
{
  "model": "$MODEL",
  "tool_execution_mode": "server",
  "tool_choice": "required",
  "tools": [
    {"type":"function","function":{"name":"read_file","description":"Read a file","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
    {"type":"function","function":{"name":"write_file","description":"Write a file","parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"},"create_dirs":{"type":"boolean"}},"required":["path","content"]}}},
    {"type":"function","function":{"name":"exec_command","description":"Run a command","parameters":{"type":"object","properties":{"command":{"type":"string"},"cwd":{"type":"string"},"timeout_seconds":{"type":"integer"}},"required":["command"]}}}
  ],
  "messages": [
    {"role":"system","content":"Use the provided tools. Update test-app/index.html so the title is NUC Agent Demo and the page says Build from prompt succeeded. Update test-app/src/main.js only if needed. Run npm run build in test-app before answering. Reply briefly with whether the build passed."},
    {"role":"user","content":"Make the requested edits in test-app and verify with a build."}
  ]
}
JSON
)

raw_agentic=$(curl -k -sS "$BASE_URL/v1/chat/completions" \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d "$AGENTIC_PAYLOAD" \
  -w '\nHTTP_STATUS:%{http_code}\n') || {
    fail "Agentic prompt build" "network/curl error"
    raw_agentic=""
  }

if [[ -n "${raw_agentic:-}" ]]; then
  status=$(echo "$raw_agentic" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  agentic_body=$(echo "$raw_agentic" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- RESPONSE: Agentic prompt build (HTTP $status) ---"
    echo "$agentic_body" | jq . 2>/dev/null || echo "$agentic_body"
  fi

  if [[ "$status" != "200" ]]; then
    fail "Agentic prompt build" "HTTP $status"
  elif echo "$agentic_body" | jq -e '
    .choices[0].finish_reason == "stop"
    and (.choices[0].message.content | type == "string")
    and ((.choices[0].message.content | length) > 0)
  ' >/dev/null 2>&1; then
    pass "Agentic prompt build"
  else
    fail "Agentic prompt build" "final answer was empty or invalid"
  fi
fi

# 41) Browser open
run_json_test \
  "Browser open" \
  "/v1/tools/browser_open" \
  '{"url":"https://example.com","max_links":5,"max_chars":1200}' \
  '.status_code == 200 and (.title == "Example Domain") and (.headings | type == "array" and length > 0) and (.links | type == "array" and length > 0) and (.main_text | contains("documentation examples"))'

# 42) Browser extract
run_json_test \
  "Browser extract" \
  "/v1/tools/browser_extract" \
  '{"url":"https://example.com","strategy":"main","max_chars":1200}' \
  '.status_code == 200 and (.title == "Example Domain") and (.text | contains("documentation examples"))'

# 43) Browser search
run_json_test \
  "Browser search" \
  "/v1/tools/browser_search" \
  '{"query":"Example Domain","domains":["example.com"],"provider":"duckduckgo","max_results":3}' \
  '.provider == "duckduckgo" and (.domains | type == "array" and length == 1) and (.results | type == "array" and length > 0)'

# 44) Weather tool
run_json_test \
  "Weather tool" \
  "/v1/tools/weather" \
  '{"location":"Madrid"}' \
  '.source == "wttr.in" and (.current.temp_c | type == "string") and (.forecast | type == "array" and length > 0)'

# 45) Time tool
run_json_test \
  "Time tool" \
  "/v1/tools/time" \
  '{"timezone":"Europe/Madrid"}' \
  '.timezone == "Europe/Madrid" and (.iso | type == "string") and (.weekday | type == "string")'

# 46) Calendar tool
run_json_test \
  "Calendar tool" \
  "/v1/tools/calendar" \
  '{"year":2026,"month":4}' \
  '.year == 2026 and .month == 4 and (.rendered_month | type == "string" and contains("April 2026"))'

# 47) Fetch URL tool
run_json_test \
  "Fetch URL tool" \
  "/v1/tools/fetch_url" \
  '{"url":"https://wttr.in/Madrid?format=j1","mode":"json"}' \
  '(.status_code == 200) and (.json.current_condition | type == "array" and length > 0)'

# 48) News search tool
run_json_test \
  "News search tool" \
  "/v1/tools/news_search" \
  '{"query":"AI regulation Europe","max_results":3,"language":"en","country":"US"}' \
  '.provider == "google_news_rss" and (.results | type == "array" and length > 0)'

# 49) Geocode tool
run_json_test \
  "Geocode tool" \
  "/v1/tools/geocode" \
  '{"query":"Atocha Madrid","max_results":2}' \
  '.provider == "nominatim" and (.results | type == "array" and length > 0)'

# 28) HTTP request tool
run_json_test \
  "HTTP request tool" \
  "/v1/tools/http_request" \
  '{"url":"https://wttr.in/Madrid?format=j1","method":"GET","mode":"json"}' \
  '.status_code == 200 and .method == "GET" and (.json.current_condition | type == "array")'

# 29) OpenAPI spec
run_get_test \
  "OpenAPI spec" \
  "/v1/openapi.json" \
  '(.openapi | type == "string")
    and (.paths["/v1/tools/weather"] != null)
    and (.paths["/v1/tools/python"] != null)
    and (.paths["/v1/tools/browser_search"] != null)
    and (.paths["/v1/tools/browser_open"] != null)
    and (.paths["/v1/tools/browser_extract"] != null)
    and (.paths["/v1/tools/list_files"] != null)
    and (.paths["/v1/tools/exec_command"] != null)'

# 30) Safe shell tool
run_json_test \
  "Safe shell tool" \
  "/v1/tools/shell_safe" \
  '{"command":"date -Iseconds","workdir":"/tmp"}' \
  '.returncode == 0 and (.stdout | type == "string" and length > 0)'

# 31) Web search tool
run_json_test \
  "Web search tool" \
  "/v1/tools/web_search" \
  '{"query":"OpenAI API latest models","provider":"auto","max_results":3}' \
  '(.results | type == "array" and length > 0) and (.provider == "google" or .provider == "duckduckgo")'

# 32) Unauthorized request should fail
run_status_test \
  "Unauthorized /v1/models" \
  "/v1/models" \
  "401" \
  "Bearer definitely-invalid-key"

# 33) Metrics
TOTAL=$((TOTAL + 1))
metrics_out=$(curl -k -sS "$BASE_URL/metrics/prometheus") || {
  fail "Prometheus metrics" "network/curl error"
  metrics_out=""
}

if [[ -n "${metrics_out:-}" ]]; then
  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- RESPONSE: Metrics ---"
    echo "$metrics_out"
  fi

  if echo "$metrics_out" | "${FILTER_CMD[@]}" "llm_tool|llm_requests_total|llm_latency_p95" >/dev/null 2>&1; then
    pass "Prometheus metrics"
  else
    fail "Prometheus metrics" "expected metric lines not found"
  fi
fi

echo
echo "Summary: total=$TOTAL ok=$PASSED fail=$FAILED"

if [[ $FAILED -gt 0 ]]; then
  exit 1
fi
