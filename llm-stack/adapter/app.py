import asyncio
import heapq
import json
import logging
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import state
from auth_security import (
    API_KEY_SALT_ENV,
    API_KEY_SECRET_ENV,
    get_required_env,
    migrate_legacy_plaintext_keys,
)
from config import (
    CHAT_CONCURRENCY,
    DEFAULT_MODEL,
    EMBED_CONCURRENCY,
    OLLAMA_URL,
    QUEUE_TIMEOUT,
    TOOL_EXECUTION_MODE,
)
from openai_compat import (
    build_chat_completion_response,
    build_effective_tools,
    make_stream_chunk,
    normalize_execution_mode,
    normalize_openai_messages,
    normalize_tool_choice,
    run_chat_with_native_tools,
    to_ollama_messages,
)
from tooling import TOOL_REGISTRY, build_all_tool_specs, execute_tool_call


app = FastAPI()
logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)


@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/v1"):
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "Missing API key"})

        token = auth_header.split(" ", 1)[1]
        if not state.valid_key(token):
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})

        request.state.api_key = token

    return await call_next(request)


async def chat_scheduler():
    while True:
        await asyncio.sleep(0.001)
        async with state.chat_lock:
            if not state.chat_queue or state.chat_active >= CHAT_CONCURRENCY:
                continue

            _, _, future = heapq.heappop(state.chat_queue)
            if not future.done():
                state.chat_active += 1
                state.METRICS["chat_active"] = state.chat_active
                future.set_result(True)

            state.METRICS["chat_queue"] = len(state.chat_queue)


async def embed_scheduler():
    while True:
        await asyncio.sleep(0.001)
        async with state.embed_lock:
            if not state.embed_queue or state.embed_active >= EMBED_CONCURRENCY:
                continue

            _, _, future = heapq.heappop(state.embed_queue)
            if not future.done():
                state.embed_active += 1
                state.METRICS["embed_active"] = state.embed_active
                future.set_result(True)

            state.METRICS["embed_queue"] = len(state.embed_queue)


@app.on_event("startup")
async def startup():
    get_required_env(API_KEY_SECRET_ENV)
    get_required_env(API_KEY_SALT_ENV)
    migrate_legacy_plaintext_keys(state.conn)
    asyncio.create_task(chat_scheduler())
    asyncio.create_task(embed_scheduler())


@app.get("/metrics")
async def metrics():
    return {
        **state.METRICS,
        "wait_p95": state.percentile(state.METRICS["wait_time"], 95),
        "latency_p95": state.percentile(state.METRICS["latency"], 95),
        "tool_latency_p95": state.percentile(state.METRICS["tool_latency"], 95),
    }


@app.get("/metrics/prometheus")
async def metrics_prom():
    tool_calls_lines = []
    for tool_name, value in sorted(state.METRICS["tool_calls_total"].items()):
        tool_calls_lines.append(f'llm_tool_calls_total{{tool="{tool_name}"}} {value}')

    tool_errors_lines = []
    for tool_name, value in sorted(state.METRICS["tool_errors_total"].items()):
        tool_errors_lines.append(f'llm_tool_errors_total{{tool="{tool_name}"}} {value}')

    tool_status_lines = []
    for key, value in sorted(state.METRICS["tool_calls_by_status"].items()):
        tool_name, status = key.split("|", 1)
        tool_status_lines.append(
            f'llm_tool_calls_by_status_total{{tool="{tool_name}",status="{status}"}} {value}'
        )

    body = f"""# HELP llm_requests_total Total number of requests
# TYPE llm_requests_total counter
llm_requests_total {state.METRICS['requests_total']}

# HELP llm_chat_active Active chat requests
# TYPE llm_chat_active gauge
llm_chat_active {state.METRICS['chat_active']}

# HELP llm_embed_active Active embedding requests
# TYPE llm_embed_active gauge
llm_embed_active {state.METRICS['embed_active']}

# HELP llm_tokens_streamed_total Total streamed tokens
# TYPE llm_tokens_streamed_total counter
llm_tokens_streamed_total {state.METRICS['tokens_streamed']}

# HELP llm_tool_requests_total Total tool execution attempts
# TYPE llm_tool_requests_total counter
llm_tool_requests_total {state.METRICS['tool_requests_total']}

# HELP llm_tool_calls_by_status_total Tool calls by execution status
# TYPE llm_tool_calls_by_status_total counter

# HELP llm_wait_p95 Queue wait p95
# TYPE llm_wait_p95 gauge
llm_wait_p95 {state.percentile(state.METRICS["wait_time"], 95)}

# HELP llm_latency_p95 Latency p95
# TYPE llm_latency_p95 gauge
llm_latency_p95 {state.percentile(state.METRICS["latency"], 95)}

# HELP llm_tool_latency_p95 Tool latency p95
# TYPE llm_tool_latency_p95 gauge
llm_tool_latency_p95 {state.percentile(state.METRICS["tool_latency"], 95)}
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
    state.METRICS["requests_total"] += 1

    api_key = request.state.api_key
    priority = state.get_priority(request, api_key)

    start_wait = time.time()
    item = await state.enqueue(state.chat_queue, state.chat_lock, priority)

    try:
        await asyncio.wait_for(item[2], timeout=QUEUE_TIMEOUT)
    except Exception:
        state.METRICS["errors"] += 1
        return JSONResponse(status_code=429, content={"error": "queue timeout"})

    state.METRICS["wait_time"].append(time.time() - start_wait)

    model = req.get("model", DEFAULT_MODEL)
    messages = normalize_openai_messages(req.get("messages", []))
    raw_execution_mode = req.get("tool_execution_mode")
    explicit_server_requested = str(raw_execution_mode or "").strip().lower() == "server"
    execution_mode = normalize_execution_mode(raw_execution_mode if raw_execution_mode is not None else TOOL_EXECUTION_MODE)
    requested_tools = req.get("tools") or []
    requested_tool_names = []
    if isinstance(requested_tools, list):
        for tool in requested_tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") or {}
            name = fn.get("name")
            if isinstance(name, str) and name:
                requested_tool_names.append(name)
    tools = build_effective_tools(
        requested_tools,
        req.get("functions"),
        execution_mode,
        allow_auto_server_tools=explicit_server_requested,
    )
    effective_tool_names = [tool.get("function", {}).get("name") for tool in tools if isinstance(tool, dict)]
    logger.info(
        "chat_request mode_raw=%s mode_effective=%s requested_tools=%s effective_tools=%s tool_choice=%s stream=%s",
        raw_execution_mode,
        execution_mode,
        requested_tool_names,
        effective_tool_names,
        req.get("tool_choice", "auto"),
        bool(req.get("stream", False)),
    )
    tool_choice = normalize_tool_choice(req.get("tool_choice", "auto"), tools)
    stream = bool(req.get("stream", False))

    start_time = time.time()

    # Create session for trazabilidad
    session = state.create_session(model)
    request_id = session["request_id"]

    try:
        if tools:
            try:
                final_message, _, _, finish_reason = await run_chat_with_native_tools(
                    model=model,
                    messages=messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    execution_mode=execution_mode,
                    request_id=request_id,
                )
            except RuntimeError as exc:
                state.METRICS["errors"] += 1
                state.close_session(request_id, error=str(exc))
                return JSONResponse(status_code=500, content={"error": str(exc)})
            except httpx.HTTPError as exc:
                state.METRICS["errors"] += 1
                state.close_session(request_id, error=f"ollama error: {exc}")
                return JSONResponse(status_code=502, content={"error": f"ollama error: {exc}"})

            state.close_session(request_id, final_result=final_message)

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
                            state.METRICS["tokens_streamed"] += 1
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
        state.chat_active = max(state.chat_active - 1, 0)
        state.METRICS["chat_active"] = state.chat_active
        state.METRICS["latency"].append(time.time() - start_time)


@app.post("/v1/embeddings")
async def embeddings(request: Request, req: dict):
    api_key = request.state.api_key
    priority = state.get_priority(request, api_key)

    item = await state.enqueue(state.embed_queue, state.embed_lock, priority)

    try:
        await asyncio.wait_for(item[2], timeout=QUEUE_TIMEOUT)
    except Exception:
        state.METRICS["errors"] += 1
        return JSONResponse(status_code=429, content={"error": "queue timeout"})

    try:
        async with httpx.AsyncClient(timeout=180.0) as client:
            response = await client.post(f"{OLLAMA_URL}/api/embeddings", json=req)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError as exc:
        state.METRICS["errors"] += 1
        return JSONResponse(status_code=502, content={"error": f"ollama error: {exc}"})
    finally:
        state.embed_active = max(state.embed_active - 1, 0)
        state.METRICS["embed_active"] = state.embed_active


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


@app.post("/v1/tools/browser_search")
async def browser_search(req: dict):
    return await execute_tool_call("browser_search", req)


@app.post("/v1/tools/browser_open")
async def browser_open(req: dict):
    return await execute_tool_call("browser_open", req)


@app.post("/v1/tools/browser_extract")
async def browser_extract(req: dict):
    return await execute_tool_call("browser_extract", req)


@app.post("/v1/tools/python")
async def python_tool(req: dict):
    return await execute_tool_call("python", req)


@app.post("/v1/tools/save_file")
async def save_file_tool(req: dict):
    return await execute_tool_call("save_file", req)


@app.post("/v1/tools/fetch_url")
async def fetch_url_tool(req: dict):
    return await execute_tool_call("fetch_url", req)


@app.post("/v1/tools/weather")
async def weather_tool(req: dict):
    return await execute_tool_call("weather", req)


@app.post("/v1/tools/time")
async def time_tool(req: dict):
    return await execute_tool_call("time", req)


@app.post("/v1/tools/calendar")
async def calendar_tool(req: dict):
    return await execute_tool_call("calendar", req)


@app.post("/v1/tools/news_search")
async def news_search_tool(req: dict):
    return await execute_tool_call("news_search", req)


@app.post("/v1/tools/geocode")
async def geocode_tool(req: dict):
    return await execute_tool_call("geocode", req)


@app.post("/v1/tools/http_request")
async def http_request_tool(req: dict):
    return await execute_tool_call("http_request", req)


@app.post("/v1/tools/sqlite_query")
async def sqlite_query_tool(req: dict):
    return await execute_tool_call("sqlite_query", req)


@app.post("/v1/tools/shell_safe")
async def shell_safe_tool(req: dict):
    return await execute_tool_call("shell_safe", req)


@app.post("/v1/tools/calendar_events")
async def calendar_events_tool(req: dict):
    return await execute_tool_call("calendar_events", req)


@app.post("/v1/tools/list_files")
async def list_files_tool(req: dict):
    return await execute_tool_call("list_files", req)


@app.post("/v1/tools/read_file")
async def read_file_tool(req: dict):
    return await execute_tool_call("read_file", req)


@app.post("/v1/tools/write_file")
async def write_file_tool(req: dict):
    return await execute_tool_call("write_file", req)


@app.post("/v1/tools/patch_file")
async def patch_file_tool(req: dict):
    return await execute_tool_call("patch_file", req)


@app.post("/v1/tools/mkdir")
async def mkdir_tool(req: dict):
    return await execute_tool_call("mkdir", req)


@app.post("/v1/tools/exec_command")
async def exec_command_tool(req: dict):
    return await execute_tool_call("exec_command", req)


@app.get("/v1/openapi.json")
async def openapi_spec():
    paths = {}
    for tool_name in TOOL_REGISTRY.keys():
        paths[f"/v1/tools/{tool_name}"] = {
            "post": {
                "operationId": tool_name,
                "summary": TOOL_REGISTRY[tool_name]["description"],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": TOOL_REGISTRY[tool_name]["parameters"]
                        }
                    },
                },
            }
        }

    return {
        "openapi": "3.0.0",
        "info": {
            "title": "Local LLM Tools API",
            "version": "2.0.0",
        },
        "paths": paths,
    }


@app.get("/v1/sessions/{request_id}")
async def get_session(request_id: str):
    """Return the full session diagnostic record for a request_id."""
    session = state.get_session(request_id)
    if session is None:
        return JSONResponse(status_code=404, content={"error": "session not found or expired"})
    return JSONResponse(content=session)
