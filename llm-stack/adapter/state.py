import asyncio
import heapq
import os
import sqlite3
from typing import List, Optional, Tuple

from fastapi import Request

from auth_security import derive_api_key_hash, ensure_api_keys_schema
from config import AUTH_DB_PATH, FILES_DIR, PRIORITY_MAP


os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

conn = sqlite3.connect(AUTH_DB_PATH, check_same_thread=False)
ensure_api_keys_schema(conn)


def get_api_key_priority(token: str) -> Optional[str]:
    key_hash = derive_api_key_hash(token)
    cur = conn.cursor()
    cur.execute("SELECT priority FROM api_keys WHERE key_hash = ?", (key_hash,))
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


def percentile(data, p: int) -> float:
    if not data:
        return 0
    sorted_data = sorted(data)
    k = int(len(sorted_data) * p / 100)
    return sorted_data[min(k, len(sorted_data) - 1)]


def bump_metric_dict(name: str, key: str) -> None:
    METRICS[name][key] = METRICS[name].get(key, 0) + 1


def bump_tool_status_metric(tool_name: str, status: str) -> None:
    bump_metric_dict("tool_calls_by_status", f"{tool_name}|{status}")


def get_priority(request: Request, api_key: str) -> int:
    header = request.headers.get("X-Priority")
    if header in PRIORITY_MAP:
        return PRIORITY_MAP[header]

    if request.url.path.endswith("/embeddings"):
        return PRIORITY_MAP["low"]

    db_priority = get_api_key_priority(api_key)
    return PRIORITY_MAP.get(db_priority, 1)


async def enqueue(queue, lock, priority):
    global counter

    future = asyncio.get_event_loop().create_future()
    counter += 1
    item = (priority, counter, future)

    async with lock:
        heapq.heappush(queue, item)

    return item
