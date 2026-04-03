#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${BASE_URL:-https://TU-NUC}"
API_KEY="${API_KEY:-TU_API_KEY}"
MODEL="${MODEL:-qwen2.5-coder:7b}"
VERBOSE=0

usage() {
  cat <<'USAGE'
Usage:
  BASE_URL="https://nuc" API_KEY="<key>" bash /home/tupolev/e2e.sh [-vv]

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
    fail "$name" "HTTP $status: $(echo "$body" | tr '\n' ' ' | cut -c1-220)"
    return
  fi

  if echo "$body" | jq -e "$jq_expr" >/dev/null 2>&1; then
    pass "$name"
  else
    fail "$name" "JSON validation failed for jq expression: $jq_expr"
  fi
}

# 1) Smoke models
TOTAL=$((TOTAL + 1))
raw_models=$(curl -k -sS "$BASE_URL/v1/models" \
  -H "Authorization: Bearer $API_KEY" \
  -w '\nHTTP_STATUS:%{http_code}\n') || {
    fail "Smoke /v1/models" "network/curl error"
    raw_models=""
  }

if [[ -n "${raw_models:-}" ]]; then
  status=$(echo "$raw_models" | awk -F: '/^HTTP_STATUS:/{print $2}' | tail -n1)
  body=$(echo "$raw_models" | sed '/^HTTP_STATUS:/d')

  if [[ $VERBOSE -eq 1 ]]; then
    echo
    echo "--- RESPONSE: Smoke /v1/models (HTTP $status) ---"
    echo "$body" | jq . 2>/dev/null || echo "$body"
  fi

  if [[ "$status" != "200" ]]; then
    fail "Smoke /v1/models" "HTTP $status"
  elif echo "$body" | jq -e '.object == "list" and (.data | type == "array")' >/dev/null 2>&1; then
    pass "Smoke /v1/models"
  else
    fail "Smoke /v1/models" "unexpected response structure"
  fi
fi

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

# 6) Metrics
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
