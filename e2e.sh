#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://TU-NUC}"
API_KEY="${API_KEY:-TU_API_KEY}"
MODEL="${MODEL:-qwen2.5-coder:7b}"
VERBOSE=0

usage() {
  cat <<'USAGE'
Usage:
  BASE_URL="https://nuc" API_KEY="<key>" bash e2e.sh [-vv]

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

stream_out=$(curl -k -sS -N --max-time 45 "$BASE_URL/v1/chat/completions" \
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

# 4) Tools in client mode (turn 1)
TOTAL=$((TOTAL + 1))
PAYLOAD_CLIENT=$(cat <<JSON
{
  "model": "$MODEL",
  "tool_execution_mode": "client",
  "tools": [
    {"type":"function","function":{"name":"python","description":"Run Python","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}}
  ],
  "messages": [{"role":"user","content":"Use python to compute 19*23."}]
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
  elif echo "$client_body" | jq -e '.choices[0].finish_reason == "tool_calls" and (.choices[0].message.tool_calls | type == "array") and (.choices[0].message.tool_calls | length > 0)' >/dev/null 2>&1; then
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
    {"type":"function","function":{"name":"python","description":"Run Python","parameters":{"type":"object","properties":{"code":{"type":"string"}},"required":["code"]}}}
  ],
  "messages": [
    {"role":"user","content":"Use python to compute 19*23."},
    {"role":"assistant","content":null,"tool_calls":[${CALL_OBJ}]},
    {"role":"tool","tool_call_id":"${CALL_ID}","name":"python","content":"{\"stdout\":\"437\\n\",\"stderr\":\"\",\"returncode\":0}"}
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

# 6) Tools catalog
run_get_test \
  "Tools catalog" \
  "/v1/tools" \
  '.data | type == "array" and (map(.function.name) | index("python")) != null and (map(.function.name) | index("weather")) != null and (map(.function.name) | index("news_search")) != null'

# 7) Weather tool
run_json_test \
  "Weather tool" \
  "/v1/tools/weather" \
  '{"location":"Madrid"}' \
  '.source == "wttr.in" and (.current.temp_c | type == "string") and (.forecast | type == "array" and length > 0)'

# 8) Time tool
run_json_test \
  "Time tool" \
  "/v1/tools/time" \
  '{"timezone":"Europe/Madrid"}' \
  '.timezone == "Europe/Madrid" and (.iso | type == "string") and (.weekday | type == "string")'

# 9) Calendar tool
run_json_test \
  "Calendar tool" \
  "/v1/tools/calendar" \
  '{"year":2026,"month":4}' \
  '.year == 2026 and .month == 4 and (.rendered_month | type == "string" and contains("April 2026"))'

# 10) Fetch URL tool
run_json_test \
  "Fetch URL tool" \
  "/v1/tools/fetch_url" \
  '{"url":"https://wttr.in/Madrid?format=j1","mode":"json"}' \
  '(.status_code == 200) and (.content_type | startswith("application/json"))'

# 11) News search tool
run_json_test \
  "News search tool" \
  "/v1/tools/news_search" \
  '{"query":"AI regulation Europe","max_results":3,"language":"en","country":"US"}' \
  '.provider == "google_news_rss" and (.results | type == "array" and length > 0)'

# 12) Geocode tool
run_json_test \
  "Geocode tool" \
  "/v1/tools/geocode" \
  '{"query":"Atocha Madrid","max_results":2}' \
  '.provider == "nominatim" and (.results | type == "array" and length > 0)'

# 13) HTTP request tool
run_json_test \
  "HTTP request tool" \
  "/v1/tools/http_request" \
  '{"url":"https://wttr.in/Madrid?format=j1","method":"GET","mode":"json"}' \
  '.status_code == 200 and .method == "GET" and (.json.current_condition | type == "array")'

# 14) OpenAPI spec
run_get_test \
  "OpenAPI spec" \
  "/v1/openapi.json" \
  '(.openapi | type == "string") and (.paths["/v1/tools/weather"] != null) and (.paths["/v1/tools/python"] != null)'

# 15) Safe shell tool
run_json_test \
  "Safe shell tool" \
  "/v1/tools/shell_safe" \
  '{"command":"date -Iseconds","workdir":"/tmp"}' \
  '.returncode == 0 and (.stdout | type == "string" and length > 0)'

# 16) Web search tool
run_json_test \
  "Web search tool" \
  "/v1/tools/web_search" \
  '{"query":"OpenAI API latest models","provider":"auto","max_results":3}' \
  '(.results | type == "array" and length > 0) and (.provider == "google" or .provider == "duckduckgo")'

# 17) Unauthorized request should fail
run_status_test \
  "Unauthorized /v1/models" \
  "/v1/models" \
  "401" \
  "Bearer definitely-invalid-key"

# 18) Metrics
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
