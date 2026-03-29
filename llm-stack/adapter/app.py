from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse, Response
import httpx
import json
import asyncio
import time
import heapq
import sqlite3
import uuid
import os
import subprocess

app = FastAPI()

OLLAMA_URL = "http://host.docker.internal:11434"

CHAT_CONCURRENCY = 2
EMBED_CONCURRENCY = 1
QUEUE_TIMEOUT = 30

PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}

# =========================
# DB
# =========================
os.makedirs("/data", exist_ok=True)
conn = sqlite3.connect("/data/auth.db", check_same_thread=False)

def get_api_key_priority(token):
    cur = conn.cursor()
    cur.execute("SELECT priority FROM api_keys WHERE key = ?", (token,))
    row = cur.fetchone()
    return row[0] if row else None

def valid_key(token):
    return get_api_key_priority(token) is not None

# =========================
# STATE
# =========================
chat_active = 0
embed_active = 0

chat_queue = []
embed_queue = []

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
    "latency": []
}

# =========================
# TOOLS
# =========================
async def run_tool(name, args):
    if name == "web_search":
        async with httpx.AsyncClient() as client:
            r = await client.get(
                "https://duckduckgo.com/html/",
                params={"q": args.get("query")}
            )
        return {"result": r.text[:2000]}

    if name == "python":
        result = subprocess.run(
            ["python3", "-c", args.get("code", "")],
            capture_output=True,
            text=True
        )
        return {"stdout": result.stdout, "stderr": result.stderr}

    return {"error": "tool not found"}

# =========================
# TOOL LOOP
# =========================
async def run_with_tools(messages, model):
    async with httpx.AsyncClient(timeout=None) as client:
        for _ in range(5):
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False
                }
            )

            data = r.json()
            content = data["message"]["content"]

            try:
                parsed = json.loads(content)
                if "tool_calls" not in parsed:
                    return content
                tool_calls = parsed["tool_calls"]
            except:
                return content

            for call in tool_calls:
                result = await run_tool(call.get("name"), call.get("arguments", {}))

                messages.append({
                    "role": "assistant",
                    "content": json.dumps(call)
                })

                messages.append({
                    "role": "tool",
                    "content": json.dumps(result)
                })

        return "Max tool iterations reached"

# =========================
# UTILS
# =========================
def percentile(data, p):
    if not data:
        return 0
    data = sorted(data)
    k = int(len(data) * p / 100)
    return data[min(k, len(data)-1)]

def get_priority(request: Request, api_key: str):
    header = request.headers.get("X-Priority")
    if header in PRIORITY_MAP:
        return PRIORITY_MAP[header]

    if request.url.path.endswith("/embeddings"):
        return PRIORITY_MAP["low"]

    db_priority = get_api_key_priority(api_key)
    return PRIORITY_MAP.get(db_priority, 1)

# =========================
# AUTH
# =========================
@app.middleware("http")
async def auth(request: Request, call_next):
    if request.url.path.startswith("/v1"):
        auth = request.headers.get("Authorization")

        if not auth or not auth.startswith("Bearer "):
            return JSONResponse(status_code=401, content={"error": "Missing API key"})

        token = auth.split(" ")[1]

        if not valid_key(token):
            return JSONResponse(status_code=401, content={"error": "Invalid API key"})

        request.state.api_key = token

    return await call_next(request)

# =========================
# QUEUE
# =========================
async def enqueue(queue, lock, priority):
    global counter

    future = asyncio.get_event_loop().create_future()
    counter += 1

    item = (priority, counter, future)

    async with lock:
        heapq.heappush(queue, item)

    return item

# =========================
# SCHEDULERS
# =========================
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

# =========================
# CHAT
# =========================
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
    except:
        METRICS["errors"] += 1
        return JSONResponse(status_code=429, content={"error": "queue timeout"})

    METRICS["wait_time"].append(time.time() - start_wait)

    model = req.get("model", "qwen2.5-coder:7b")
    messages = req.get("messages", [])
    use_tools = bool(req.get("tools"))

    start_time = time.time()

    try:
        if use_tools:
            result = await run_with_tools(messages, model)

            return JSONResponse({
                "id": f"chatcmpl-{uuid.uuid4().hex}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": result
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            })

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "POST",
                f"{OLLAMA_URL}/api/chat",
                json={"model": model, "messages": messages, "stream": True}
            ) as r:

                async def generator():
                    async for line in r.aiter_lines():
                        if not line:
                            continue

                        try:
                            data = json.loads(line)
                        except:
                            continue

                        token = data.get("message", {}).get("content")

                        if token:
                            METRICS["tokens_streamed"] += 1

                            chunk = {
                                "id": f"chatcmpl-{uuid.uuid4().hex}",
                                "object": "chat.completion.chunk",
                                "choices": [{
                                    "delta": {"content": token},
                                    "index": 0,
                                    "finish_reason": None
                                }]
                            }

                            yield "data: " + json.dumps(chunk) + "\n\n"

                        if data.get("done"):
                            yield "data: [DONE]\n\n"
                            break

                return StreamingResponse(generator(), media_type="text/event-stream")

    finally:
        chat_active -= 1
        METRICS["chat_active"] = chat_active
        METRICS["latency"].append(time.time() - start_time)

# =========================
# METRICS
# =========================
@app.get("/metrics/prometheus")
async def metrics_prom():
    return Response(
        f"""llm_requests_total {METRICS['requests_total']}
llm_chat_active {METRICS['chat_active']}
llm_embed_active {METRICS['embed_active']}
llm_tokens_streamed_total {METRICS['tokens_streamed']}
llm_wait_p95 {percentile(METRICS['wait_time'], 95)}
llm_latency_p95 {percentile(METRICS['latency'], 95)}
""",
        media_type="text/plain"
    )

# =========================
# OPENAPI
# =========================
@app.get("/v1/openapi.json")
async def openapi_spec():
    return {
        "openapi": "3.0.0",
        "info": {"title": "LLM Tools API", "version": "1.0.0"},
        "paths": {
            "/v1/tools/web_search": {
                "post": {
                    "operationId": "web_search"
                }
            },
            "/v1/tools/python": {
                "post": {
                    "operationId": "python"
                }
            }
        }
    }
