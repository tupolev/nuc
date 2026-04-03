from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import asyncio
import heapq
import httpx
import json
import os
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

app = FastAPI()

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://host.docker.internal:11434")
FILES_DIR = os.getenv("FILES_DIR", "/data/files")
AUTH_DB_PATH = os.getenv("AUTH_DB_PATH", "/data/auth.db")

CHAT_CONCURRENCY = int(os.getenv("CHAT_CONCURRENCY", "2"))
EMBED_CONCURRENCY = int(os.getenv("EMBED_CONCURRENCY", "1"))
QUEUE_TIMEOUT = int(os.getenv("QUEUE_TIMEOUT", "30"))
TOOL_MAX_ITERATIONS = int(os.getenv("TOOL_MAX_ITERATIONS", "8"))
TOOL_ARG_MAX_LEN = int(os.getenv("TOOL_ARG_MAX_LEN", "50000"))
TOOL_OUTPUT_MAX_LEN = int(os.getenv("TOOL_OUTPUT_MAX_LEN", "100000"))
PYTHON_TIMEOUT = int(os.getenv("PYTHON_TIMEOUT", "30"))
PYTHON_CODE_MAX_LEN = int(os.getenv("PYTHON_CODE_MAX_LEN", "12000"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2.5-coder:7b")
TOOL_EXECUTION_MODE = os.getenv("TOOL_EXECUTION_MODE", "server").strip().lower()
AUTO_ENABLE_LOCAL_TOOLS = os.getenv("AUTO_ENABLE_LOCAL_TOOLS", "false").strip().lower() == "true"

PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}

os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False)


def get_api_key_priority(token: str) -> Optional[str]:
    cur = conn.cursor()
    cur.execute("SELECT priority FROM api_keys WHERE key = ?", (token,))
    row = cur.fetchone()
    return row[0] if row else None


def valid_key(token: str) -> bool:
    return get_api_key_priority(token) is not None


chat_active = 0
embed_active = 0

chat_queue: List[Tuple[int, int, asyncio.Future]] = []
embed_queue: List[Tuple[int, int, asyncio.Future]] = []

chat_lock = asyncio.Lock()
embed_lock = asyncio.Lock()

counter = 0

METRICS = {
    "requests_total": 0,
    "chat_active": 0,
    "embed_active": 0,
    "chat_queue": 0,
    "embed_queue": 0,
    "errors": 0,
    "tokens_streamed": 0,
    "wait_time": [],
    "latency": [],
    "tool_requests_total": 0,
    "tool_calls_total": {},
    "tool_errors_total": {},
    "tool_calls_by_status": {},
    "tool_latency": [],
    "tool_loop_iterations": [],
}


async def run_web_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(
            "https://duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()

    html = response.text[:TOOL_OUTPUT_MAX_LEN]
    return {
        "query": query,
        "result": html,
        "note": "Raw HTML search results truncated for size.",
    }


async def run_python(args: Dict[str, Any]) -> Dict[str, Any]:
    code = str(args.get("code", ""))
    if not code.strip():
        return {"error": "code is required"}
    if len(code) > PYTHON_CODE_MAX_LEN:
        return {"error": f"code too long (max {PYTHON_CODE_MAX_LEN} chars)"}

    result = subprocess.run(
        ["python3", "-c", code],
        capture_output=True,
        text=True,
        timeout=PYTHON_TIMEOUT,
        cwd="/tmp",
    )

    return {
        "stdout": result.stdout[:TOOL_OUTPUT_MAX_LEN],
        "stderr": result.stderr[:TOOL_OUTPUT_MAX_LEN],
        "returncode": result.returncode,
    }


async def run_save_file(args: Dict[str, Any]) -> Dict[str, Any]:
    filename = str(args.get("filename", "")).strip()
    content = str(args.get("content", ""))

    if not filename:
        return {"error": "filename is required"}

    safe_name = os.path.basename(filename)
    if not safe_name:
        return {"error": "invalid filename"}

    path = os.path.join(FILES_DIR, safe_name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    return {
        "filename": safe_name,
        "path": path,
        "bytes_written": len(content.encode("utf-8")),
    }


TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "web_search": {
        "description": "Search the web and return raw HTML results. Use this when fresh web information is needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to submit."}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": run_web_search,
    },
    "python": {
        "description": "Execute Python code locally and return stdout, stderr and return code. Use only for computation or text/data processing tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute."}
            },
            "required": ["code"],
            "additionalProperties": False,
        },
        "handler": run_python,
    },
    "save_file": {
        "description": "Save a UTF-8 text file to local disk. Use only when the user explicitly asks to create or save a file.",
        "parameters": {
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Base file name, for example notes.txt.",
                },
                "content": {
                    "type": "string",
                    "description": "Full UTF-8 file content.",
                },
            },
            "required": ["filename", "content"],
            "additionalProperties": False,
        },
        "handler": run_save_file,
    },
}


def percentile(data: List[float], p: int) -> float:
    if not data:
        return 0
    sorted_data = sorted(data)
    k = int(len(sorted_data) * p / 100)
    return sorted_data[min(k, len(sorted_data) - 1)]


def bump_metric_dict(name: str, key: str) -> None:
    METRICS[name][key] = METRICS[name].get(key, 0) + 1


def bump_tool_status_metric(tool_name: str, status: str) -> None:
    bump_metric_dict("tool_calls_by_status", f"{tool_name}|{status}")


def safe_json_dumps(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) > TOOL_OUTPUT_MAX_LEN:
        return text[:TOOL_OUTPUT_MAX_LEN]
    return text


def parse_json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value

    if isinstance(value, str):
        if len(value) > TOOL_ARG_MAX_LEN:
            raise ValueError(f"tool arguments too long (max {TOOL_ARG_MAX_LEN} chars)")
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("tool arguments must decode to an object")
        return parsed

    raise ValueError("tool arguments must be a JSON object or JSON string")


def validate_args_against_schema(name: str, args: Dict[str, Any]) -> None:
    spec = TOOL_REGISTRY[name]
    schema = spec["parameters"]
    props = schema.get("properties", {})
    required = schema.get("required", [])
    additional = schema.get("additionalProperties", True)

    for field in required:
        if field not in args:
            raise ValueError(f"missing required field '{field}'")

    if not additional:
        unknown = [key for key in args.keys() if key not in props]
        if unknown:
            raise ValueError(f"unknown fields: {', '.join(unknown)}")

    for field, definition in props.items():
        if field not in args:
            continue
        if definition.get("type") == "string" and not isinstance(args[field], str):
            raise ValueError(f"field '{field}' must be a string")


def get_priority(request: Request, api_key: str) -> int:
    header = request.headers.get("X-Priority")
    if header in PRIORITY_MAP:
        return PRIORITY_MAP[header]

    if request.url.path.endswith("/embeddings"):
        return PRIORITY_MAP["low"]

    db_priority = get_api_key_priority(api_key)
    return PRIORITY_MAP.get(db_priority, 1)


def build_tool_spec(name: str) -> Dict[str, Any]:
    spec = TOOL_REGISTRY[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": spec["description"],
            "parameters": spec["parameters"],
        },
    }


def build_all_tool_specs() -> List[Dict[str, Any]]:
    return [build_tool_spec(name) for name in TOOL_REGISTRY.keys()]


def normalize_execution_mode(raw_mode: Any) -> str:
    mode = str(raw_mode or TOOL_EXECUTION_MODE).strip().lower()
    if mode not in {"server", "client"}:
        return "server"
    return mode


def convert_legacy_functions_to_tools(functions: Any) -> List[Dict[str, Any]]:
    tools = []
    if not isinstance(functions, list):
        return tools

    for fn in functions:
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                },
            }
        )

    return tools


def normalize_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not tools:
        return []

    normalized = []
    seen = set()

    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue

        fn = tool.get("function") or {}
        name = fn.get("name")
        if not name or name not in TOOL_REGISTRY or name in seen:
            continue

        normalized.append(build_tool_spec(name))
        seen.add(name)

    return normalized


def build_effective_tools(requested_tools: Any, legacy_functions: Any) -> List[Dict[str, Any]]:
    candidate_tools = requested_tools or []
    if not candidate_tools and legacy_functions:
        candidate_tools = convert_legacy_functions_to_tools(legacy_functions)

    normalized = normalize_openai_tools(candidate_tools)
    if normalized:
        return normalized

    if AUTO_ENABLE_LOCAL_TOOLS:
        return build_all_tool_specs()

    return []


def normalize_tool_choice(raw_tool_choice: Any, available_tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    allowed_names = {tool["function"]["name"] for tool in available_tools}

    if raw_tool_choice is None:
        return {"mode": "auto", "forced_name": None}

    if isinstance(raw_tool_choice, str):
        value = raw_tool_choice.strip().lower()
        if value in {"auto", "none", "required"}:
            return {"mode": value, "forced_name": None}
        return {"mode": "auto", "forced_name": None}

    if isinstance(raw_tool_choice, dict):
        tool_type = raw_tool_choice.get("type")
        fn = raw_tool_choice.get("function") or {}
        name = fn.get("name")
        if tool_type == "function" and isinstance(name, str) and name in allowed_names:
            return {"mode": "forced", "forced_name": name}

    return {"mode": "auto", "forced_name": None}


def build_tool_system_message(tool_choice: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    base_content = (
        "You may use available tools when needed. "
        "Only call a tool when it is necessary to answer accurately or perform the requested action. "
        "If you call a tool, produce valid tool calls with arguments matching the provided JSON schema. "
        "Never serialize tool calls as plain JSON text in message content. "
        "Do not invent tool results. After tool results are provided, continue and produce the final answer."
    )
    if tool_choice and tool_choice.get("mode") == "forced" and tool_choice.get("forced_name"):
        base_content += f" You must call the tool '{tool_choice['forced_name']}' before giving the final answer."
    if tool_choice and tool_choice.get("mode") == "required":
        base_content += " You must call at least one tool before giving the final answer."
    return {"role": "system", "content": base_content}


def has_system_message(messages: List[Dict[str, Any]]) -> bool:
    return any(message.get("role") == "system" for message in messages)


def normalize_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        if role not in {"system", "user", "assistant", "tool"}:
            continue

        content = message.get("content")
        if content is None:
            content = ""

        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    text_parts.append(str(item.get("text", "")))
            content = "\n".join(text_parts)
        else:
            content = str(content)

        normalized_item: Dict[str, Any] = {"role": role, "content": content}

        if role == "assistant" and message.get("tool_calls"):
            normalized_item["tool_calls"] = message["tool_calls"]

        if role == "tool":
            if message.get("name"):
                normalized_item["name"] = message["name"]
            if message.get("tool_name"):
                normalized_item["tool_name"] = message["tool_name"]
            if message.get("tool_call_id"):
                normalized_item["tool_call_id"] = message["tool_call_id"]

        normalized.append(normalized_item)

    return normalized


def to_ollama_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ollama_messages: List[Dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        item: Dict[str, Any] = {
            "role": role,
            "content": message.get("content", "") or "",
        }

        if role == "assistant" and message.get("tool_calls"):
            native_tool_calls = []
            for tool_call in message["tool_calls"]:
                if not isinstance(tool_call, dict):
                    continue
                fn = tool_call.get("function") or {}
                args = fn.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                native_tool_calls.append(
                    {
                        "function": {
                            "name": fn.get("name", ""),
                            "arguments": args,
                        }
                    }
                )
            if native_tool_calls:
                item["tool_calls"] = native_tool_calls

        if role == "tool":
            if message.get("tool_name"):
                item["name"] = message["tool_name"]
            elif message.get("name"):
                item["name"] = message["name"]

        ollama_messages.append(item)

    return ollama_messages


def make_openai_tool_call(raw_tool_call: Dict[str, Any]) -> Dict[str, Any]:
    fn = raw_tool_call.get("function", {}) or {}
    name = fn.get("name", "")
    arguments = fn.get("arguments", {})

    if isinstance(arguments, str):
        arguments_str = arguments
    else:
        arguments_str = json.dumps(arguments, ensure_ascii=False)

    return {
        "id": raw_tool_call.get("id") or f"call_{uuid.uuid4().hex}",
        "type": "function",
        "function": {
            "name": name,
            "arguments": arguments_str,
        },
    }


def strip_wrapping_code_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if len(lines) >= 2 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()

    return stripped


def choose_tool_name(
    requested_name: Any,
    available_tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    available_names = [tool["function"]["name"] for tool in available_tools]

    if isinstance(requested_name, str) and requested_name in available_names:
        return requested_name

    forced_name = (tool_choice or {}).get("forced_name")
    if forced_name in available_names:
        return forced_name

    if len(available_names) == 1:
        return available_names[0]

    return None


def coerce_fallback_tool_call(
    item: Dict[str, Any],
    available_tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    function_payload = item.get("function")

    if isinstance(function_payload, dict):
        name = choose_tool_name(function_payload.get("name"), available_tools, tool_choice)
        arguments = function_payload.get("arguments", {})
    else:
        name = choose_tool_name(item.get("name"), available_tools, tool_choice)
        arguments = item.get("arguments", {})

    if not name:
        return None

    return {
        "id": item.get("id") or f"call_{uuid.uuid4().hex}",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def extract_tool_calls_from_content(
    content: str,
    available_tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if not content or not available_tools:
        return []

    text = strip_wrapping_code_fence(content)
    payload: Any = None

    try:
        payload = json.loads(text)
    except Exception:
        return []

    candidate_items: List[Dict[str, Any]] = []
    if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
        candidate_items = [item for item in payload["tool_calls"] if isinstance(item, dict)]
    elif isinstance(payload, dict):
        candidate_items = [payload]
    elif isinstance(payload, list):
        candidate_items = [item for item in payload if isinstance(item, dict)]

    results = []
    for item in candidate_items:
        normalized = coerce_fallback_tool_call(item, available_tools, tool_choice)
        if normalized:
            results.append(normalized)

    return results


def extract_tool_calls(
    raw_message: Dict[str, Any],
    available_tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    raw_tool_calls = raw_message.get("tool_calls") or []
    if isinstance(raw_tool_calls, list) and raw_tool_calls:
        return [item for item in raw_tool_calls if isinstance(item, dict)]

    content = raw_message.get("content", "") or ""
    return extract_tool_calls_from_content(str(content), available_tools, tool_choice)


def build_chat_completion_response(
    model: str,
    message: Dict[str, Any],
    finish_reason: str = "stop",
) -> Dict[str, Any]:
    response_message = {
        "role": "assistant",
        "content": message.get("content", ""),
    }

    if message.get("tool_calls"):
        response_message["content"] = message.get("content") or None
        response_message["tool_calls"] = message["tool_calls"]

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": response_message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
    }


def make_stream_chunk(
    content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
) -> str:
    delta: Dict[str, Any] = {}

    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls

    chunk = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "choices": [
            {
                "delta": delta,
                "index": 0,
                "finish_reason": finish_reason,
            }
        ],
    }
    return "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"


async def execute_tool_call(name: str, raw_args: Any) -> Dict[str, Any]:
    METRICS["tool_requests_total"] += 1
    start = time.time()

    if name not in TOOL_REGISTRY:
        bump_metric_dict("tool_errors_total", name)
        bump_tool_status_metric(name, "not_found")
        return {"error": f"tool '{name}' not found"}

    try:
        args = parse_json_object(raw_args)
        validate_args_against_schema(name, args)
        result = await TOOL_REGISTRY[name]["handler"](args)

        if isinstance(result, dict) and result.get("error"):
            bump_metric_dict("tool_errors_total", name)
            bump_tool_status_metric(name, "handler_error")
        else:
            bump_metric_dict("tool_calls_total", name)
            bump_tool_status_metric(name, "ok")

        return result
    except Exception as exc:
        bump_metric_dict("tool_errors_total", name)
        bump_tool_status_metric(name, "exception")
        return {"error": str(exc)}
    finally:
        METRICS["tool_latency"].append(time.time() - start)


async def run_chat_with_native_tools(
    model: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
    execution_mode: str = "server",
    max_iterations: int = TOOL_MAX_ITERATIONS,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], int, str]:
    async with httpx.AsyncClient(timeout=180.0) as client:
        history = list(messages)
        parsed_choice = tool_choice or {"mode": "auto", "forced_name": None}

        if tools and not has_system_message(history):
            history.insert(0, build_tool_system_message(parsed_choice))

        if parsed_choice.get("mode") == "none":
            response = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={"model": model, "messages": to_ollama_messages(history), "stream": False},
            )
            response.raise_for_status()
            data = response.json()
            final_message = {
                "role": "assistant",
                "content": data.get("message", {}).get("content", "") or "",
            }
            METRICS["tool_loop_iterations"].append(0)
            return final_message, history, 1, "stop"

        seen_calls: Dict[str, int] = {}
        forced_retry_used = False

        for iteration in range(1, max_iterations + 1):
            payload: Dict[str, Any] = {
                "model": model,
                "messages": to_ollama_messages(history),
                "stream": False,
            }

            if tools:
                payload["tools"] = tools

            response = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            response.raise_for_status()
            data = response.json()

            raw_message = data.get("message", {}) or {}
            extracted_tool_calls = extract_tool_calls(raw_message, tools, parsed_choice)

            if not extracted_tool_calls:
                must_call = parsed_choice.get("mode") in {"required", "forced"}
                if must_call and not forced_retry_used:
                    forced_retry_used = True
                    forced_name = parsed_choice.get("forced_name")
                    if forced_name:
                        reminder = f"Tool call required. Call tool '{forced_name}' now with valid arguments."
                    else:
                        reminder = "Tool call required. Call at least one valid tool now."
                    history.append({"role": "system", "content": reminder})
                    continue

                METRICS["tool_loop_iterations"].append(iteration - 1 if tools else 0)
                final_message = {
                    "role": "assistant",
                    "content": raw_message.get("content", "") or "",
                }
                return final_message, history, iteration, "stop"

            openai_tool_calls = [make_openai_tool_call(tool_call) for tool_call in extracted_tool_calls]

            history.append(
                {
                    "role": "assistant",
                    "content": raw_message.get("content", "") or "",
                    "tool_calls": openai_tool_calls,
                }
            )

            if execution_mode == "client":
                METRICS["tool_loop_iterations"].append(iteration)
                return history[-1], history, iteration, "tool_calls"

            for tool_call in openai_tool_calls:
                tool_name = tool_call["function"]["name"]
                tool_args = tool_call["function"]["arguments"]
                signature = f"{tool_name}:{tool_args}"
                seen_calls[signature] = seen_calls.get(signature, 0) + 1

                if seen_calls[signature] > 2:
                    bump_metric_dict("tool_errors_total", tool_name)
                    bump_tool_status_metric(tool_name, "duplicate_blocked")
                    tool_result = {"error": "duplicate tool call blocked to prevent infinite loop"}
                else:
                    tool_result = await execute_tool_call(tool_name, tool_args)

                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "tool_name": tool_name,
                        "content": safe_json_dumps(tool_result),
                    }
                )

        raise RuntimeError("max tool iterations reached")


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/v1"):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "Missing API key"})

        token = auth_header.split(" ", 1)[1]
        if not valid_key(token):
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})

        request.state.api_key = token

    return await call_next(request)


async def enqueue(queue, lock, priority):
    global counter

    future = asyncio.get_event_loop().create_future()
    counter += 1
    item = (priority, counter, future)

    async with lock:
        heapq.heappush(queue, item)

    return item


async def chat_scheduler():
    global chat_active

    while True:
        await asyncio.sleep(0.001)
        async with chat_lock:
            if not chat_queue or chat_active >= CHAT_CONCURRENCY:
                continue

            _, _, future = heapq.heappop(chat_queue)
            if not future.done():
                chat_active += 1
                METRICS["chat_active"] = chat_active
                future.set_result(True)

            METRICS["chat_queue"] = len(chat_queue)


async def embed_scheduler():
    global embed_active

    while True:
        await asyncio.sleep(0.001)
        async with embed_lock:
            if not embed_queue or embed_active >= EMBED_CONCURRENCY:
                continue

            _, _, future = heapq.heappop(embed_queue)
            if not future.done():
                embed_active += 1
                METRICS["embed_active"] = embed_active
                future.set_result(True)

            METRICS["embed_queue"] = len(embed_queue)


@app.on_event("startup")
async def startup():
    asyncio.create_task(chat_scheduler())
    asyncio.create_task(embed_scheduler())


@app.get("/metrics")
async def metrics():
    return {
        **METRICS,
        "wait_p95": percentile(METRICS["wait_time"], 95),
        "latency_p95": percentile(METRICS["latency"], 95),
        "tool_latency_p95": percentile(METRICS["tool_latency"], 95),
    }


@app.get("/metrics/prometheus")
async def metrics_prom():
    tool_calls_lines = []
    for tool_name, value in sorted(METRICS["tool_calls_total"].items()):
        tool_calls_lines.append(f'llm_tool_calls_total{{tool="{tool_name}"}} {value}')

    tool_errors_lines = []
    for tool_name, value in sorted(METRICS["tool_errors_total"].items()):
        tool_errors_lines.append(f'llm_tool_errors_total{{tool="{tool_name}"}} {value}')

    tool_status_lines = []
    for key, value in sorted(METRICS["tool_calls_by_status"].items()):
        tool_name, status = key.split("|", 1)
        tool_status_lines.append(
            f'llm_tool_calls_by_status_total{{tool="{tool_name}",status="{status}"}} {value}'
        )

    body = f"""# HELP llm_requests_total Total number of requests
# TYPE llm_requests_total counter
llm_requests_total {METRICS['requests_total']}

# HELP llm_chat_active Active chat requests
# TYPE llm_chat_active gauge
llm_chat_active {METRICS['chat_active']}

# HELP llm_embed_active Active embedding requests
# TYPE llm_embed_active gauge
llm_embed_active {METRICS['embed_active']}

# HELP llm_tokens_streamed_total Total streamed tokens
# TYPE llm_tokens_streamed_total counter
llm_tokens_streamed_total {METRICS['tokens_streamed']}

# HELP llm_tool_requests_total Total tool execution attempts
# TYPE llm_tool_requests_total counter
llm_tool_requests_total {METRICS['tool_requests_total']}

# HELP llm_tool_calls_by_status_total Tool calls by execution status
# TYPE llm_tool_calls_by_status_total counter

# HELP llm_wait_p95 Queue wait p95
# TYPE llm_wait_p95 gauge
llm_wait_p95 {percentile(METRICS["wait_time"], 95)}

# HELP llm_latency_p95 Latency p95
# TYPE llm_latency_p95 gauge
llm_latency_p95 {percentile(METRICS["latency"], 95)}

# HELP llm_tool_latency_p95 Tool latency p95
# TYPE llm_tool_latency_p95 gauge
llm_tool_latency_p95 {percentile(METRICS["tool_latency"], 95)}
"""
    if tool_calls_lines:
        body += "\n" + "\n".join(tool_calls_lines)
    if tool_errors_lines:
        body += "\n" + "\n".join(tool_errors_lines)
    if tool_status_lines:
        body += "\n" + "\n".join(tool_status_lines)

    return Response(body + "\n", media_type="text/plain")


@app.post("/v1/chat/completions")
async def chat(request: Request, req: dict):
    global chat_active

    METRICS["requests_total"] += 1

    api_key = request.state.api_key
    priority = get_priority(request, api_key)

    start_wait = time.time()
    item = await enqueue(chat_queue, chat_lock, priority)

    try:
        await asyncio.wait_for(item[2], timeout=QUEUE_TIMEOUT)
    except Exception:
        METRICS["errors"] += 1
        return JSONResponse(status_code=429, content={"error": "queue timeout"})

    METRICS["wait_time"].append(time.time() - start_wait)

    model = req.get("model", DEFAULT_MODEL)
    messages = normalize_openai_messages(req.get("messages", []))
    tools = build_effective_tools(req.get("tools"), req.get("functions"))
    tool_choice = normalize_tool_choice(req.get("tool_choice", "auto"), tools)
    execution_mode = normalize_execution_mode(req.get("tool_execution_mode", TOOL_EXECUTION_MODE))
    stream = bool(req.get("stream", False))

    start_time = time.time()

    try:
        if tools:
            try:
                final_message, _, _, finish_reason = await run_chat_with_native_tools(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    execution_mode=execution_mode,
                )
            except RuntimeError as exc:
                METRICS["errors"] += 1
                return JSONResponse(status_code=500, content={"error": str(exc)})
            except httpx.HTTPError as exc:
                METRICS["errors"] += 1
                return JSONResponse(status_code=502, content={"error": f"ollama error: {exc}"})

            if stream:
                async def tool_stream_generator():
                    if final_message.get("tool_calls"):
                        yield make_stream_chunk(tool_calls=final_message["tool_calls"])
                    else:
                        content = final_message.get("content", "")
                        if content:
                            yield make_stream_chunk(content=content)
                    yield make_stream_chunk(finish_reason=finish_reason)
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    tool_stream_generator(),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )

            return JSONResponse(
                build_chat_completion_response(
                    model=model,
                    message=final_message,
                    finish_reason=finish_reason,
                )
            )

        if not stream:
            async with httpx.AsyncClient(timeout=180.0) as client:
                response = await client.post(
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": model,
                        "messages": to_ollama_messages(messages),
                        "stream": False,
                    },
                )
                response.raise_for_status()
                data = response.json()
                final_message = {
                    "role": "assistant",
                    "content": data.get("message", {}).get("content", "") or "",
                }
                return JSONResponse(
                    build_chat_completion_response(
                        model=model,
                        message=final_message,
                        finish_reason="stop",
                    )
                )

        async def generator():
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": model,
                        "messages": to_ollama_messages(messages),
                        "stream": True,
                    },
                ) as response:
                    response.raise_for_status()

                    async for line in response.aiter_lines():
                        if await request.is_disconnected():
                            break

                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                        except Exception:
                            continue

                        token = data.get("message", {}).get("content")
                        if token:
                            METRICS["tokens_streamed"] += 1
                            yield make_stream_chunk(content=token)

                        if data.get("done"):
                            yield make_stream_chunk(finish_reason="stop")
                            yield "data: [DONE]\n\n"
                            break

        return StreamingResponse(
            generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    finally:
        chat_active = max(chat_active - 1, 0)
        METRICS["chat_active"] = chat_active
        METRICS["latency"].append(time.time() - start_time)


@app.post("/v1/embeddings")
async def embeddings(request: Request, req: dict):
    global embed_active

    api_key = request.state.api_key
    priority = get_priority(request, api_key)

    item = await enqueue(embed_queue, embed_lock, priority)

    try:
        await asyncio.wait_for(item[2], timeout=QUEUE_TIMEOUT)
    except Exception:
        METRICS["errors"] += 1
        return JSONResponse(status_code=429, content={"error": "queue timeout"})

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(f"{OLLAMA_URL}/api/embeddings", json=req)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        METRICS["errors"] += 1
        return JSONResponse(status_code=502, content={"error": f"ollama error: {exc}"})
    finally:
        embed_active = max(embed_active - 1, 0)
        METRICS["embed_active"] = embed_active


@app.get("/v1/models")
async def models():
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.get(f"{OLLAMA_URL}/api/tags")
        response.raise_for_status()

    models_data = response.json().get("models", [])
    return {
        "object": "list",
        "data": [
            {
                "id": model_info["name"],
                "object": "model",
                "created": 0,
                "owned_by": "ollama",
            }
            for model_info in models_data
        ],
    }


@app.get("/v1/tools")
async def list_tools():
    return {"data": build_all_tool_specs()}


@app.post("/v1/tools/web_search")
async def web_search(req: dict):
    return await execute_tool_call("web_search", req)


@app.post("/v1/tools/python")
async def python_tool(req: dict):
    return await execute_tool_call("python", req)


@app.post("/v1/tools/save_file")
async def save_file_tool(req: dict):
    return await execute_tool_call("save_file", req)


@app.get("/v1/openapi.json")
async def openapi_spec():
    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Local LLM Tools API",
            "version": "2.0.0",
        },
        "paths": {
            "/v1/tools/web_search": {
                "post": {
                    "operationId": "web_search",
                    "summary": "Search the web",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": TOOL_REGISTRY["web_search"]["parameters"]
                            }
                        },
                    },
                }
            },
            "/v1/tools/python": {
                "post": {
                    "operationId": "python",
                    "summary": "Execute Python code",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": TOOL_REGISTRY["python"]["parameters"]
                            }
                        },
                    },
                }
            },
            "/v1/tools/save_file": {
                "post": {
                    "operationId": "save_file",
                    "summary": "Save a file on disk",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": TOOL_REGISTRY["save_file"]["parameters"]
                            }
                        },
                    },
                }
            },
        },
    }
