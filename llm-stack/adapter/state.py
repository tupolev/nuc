import asyncio
import heapq
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

from config import COMMAND_OUTPUT_MAX_BYTES

from auth_security import derive_api_key_hash, ensure_api_keys_schema
from config import AUTH_DB_PATH, FILES_DIR, PRIORITY_MAP


os.makedirs(os.path.dirname(AUTH_DB_PATH), exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

logger = logging.getLogger(__name__)


# In-memory session store: request_id -> session data
# Each session lives up to MAX_SESSION_AGE_SECONDS after last update
SESSION_STORE: Dict[str, Dict[str, Any]] = {}
SESSION_LISTENER: Optional[asyncio.Future] = None

# Background process store: process_id -> process data
BG_PROCESS_STORE: Dict[str, Dict[str, Any]] = {}
BG_PROCESS_LISTENER: Optional[asyncio.Future] = None

MAX_SESSION_AGE_SECONDS = 3600  # 1 hour
MAX_SESSIONS = 1000


def _generate_request_id() -> str:
    return uuid.uuid4().hex[:16]


def _cleanup_expired_sessions() -> None:
    """Remove sessions older than MAX_SESSION_AGE_SECONDS."""
    now = time.time()
    expired = [
        rid for rid, sess in SESSION_STORE.items()
        if now - sess.get("updated_at", 0) > MAX_SESSION_AGE_SECONDS
    ]
    for rid in expired:
        SESSION_STORE.pop(rid, None)


def create_session(model: str) -> Dict[str, Any]:
    """Create a new session and return its request_id."""
    _cleanup_expired_sessions()
    if len(SESSION_STORE) >= MAX_SESSIONS:
        # evict oldest
        oldest = min(SESSION_STORE.items(), key=lambda item: item[1].get("updated_at", 0))
        SESSION_STORE.pop(oldest[0], None)
    request_id = _generate_request_id()
    session = {
        "request_id": request_id,
        "model": model,
        "created_at": time.time(),
        "updated_at": time.time(),
        "active": True,
        "history": [],
        "tool_calls": [],
        "final_result": None,
        "error": None,
    }
    SESSION_STORE[request_id] = session
    return session


def get_session(request_id: str) -> Optional[Dict[str, Any]]:
    """Get session by request_id if it exists and is not expired."""
    session = SESSION_STORE.get(request_id)
    if session is None:
        return None
    if time.time() - session.get("updated_at", 0) > MAX_SESSION_AGE_SECONDS:
        SESSION_STORE.pop(request_id, None)
        return None
    return session


def update_session(request_id: str, **kwargs) -> None:
    """Update session fields."""
    if request_id in SESSION_STORE:
        SESSION_STORE[request_id].update(kwargs)
        SESSION_STORE[request_id]["updated_at"] = time.time()


def append_tool_call(request_id: str, tool_name: str, args: Dict[str, Any], result: Any, duration_ms: float) -> None:
    """Append a tool call record to the session."""
    if request_id in SESSION_STORE:
        SESSION_STORE[request_id]["tool_calls"].append({
            "name": tool_name,
            "args": sanitize_for_logging(args, tool_name),
            "result": result,
            "duration_ms": duration_ms,
        })
        SESSION_STORE[request_id]["updated_at"] = time.time()


def close_session(request_id: str, final_result: Any = None, error: str = None) -> None:
    """Mark session as closed with final result or error."""
    if request_id in SESSION_STORE:
        SESSION_STORE[request_id]["active"] = False
        SESSION_STORE[request_id]["final_result"] = final_result
        SESSION_STORE[request_id]["error"] = error
        SESSION_STORE[request_id]["updated_at"] = time.time()


# ---- Background process management ----

BG_PROCESS_MAX_AGE_SECONDS = 7200  # 2 hours
BG_PROCESS_MAX_COUNT = 50


def _generate_process_id() -> str:
    return uuid.uuid4().hex[:8]


def _cleanup_expired_processes() -> None:
    """Remove processes older than BG_PROCESS_MAX_AGE_SECONDS or finished with stale data."""
    now = time.time()
    expired = [
        pid for pid, proc in BG_PROCESS_STORE.items()
        if now - proc.get("started_at", 0) > BG_PROCESS_MAX_AGE_SECONDS
        or (proc.get("running") is False and now - proc.get("finished_at", 0) > 300)
    ]
    for pid in expired:
        BG_PROCESS_STORE.pop(pid, None)


def start_bg_process(command: List[str], cwd: str, env: Dict[str, str]) -> Dict[str, Any]:
    """Start a background process and return process info."""
    _cleanup_expired_processes()
    if len(BG_PROCESS_STORE) >= BG_PROCESS_MAX_COUNT:
        return {"error": "too many background processes, wait for some to finish"}
    try:
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except Exception as exc:
        return {"error": f"failed to start process: {exc}"}

    process_id = _generate_process_id()
    BG_PROCESS_STORE[process_id] = {
        "process_id": process_id,
        "command": command,
        "cwd": cwd,
        "pid": proc.pid,
        "started_at": time.time(),
        "running": True,
        "finished_at": None,
        "returncode": None,
        "stdout": "",
        "stderr": "",
    }
    return {"process_id": process_id, "pid": proc.pid}


def get_bg_process(process_id: str) -> Optional[Dict[str, Any]]:
    """Get process info, collecting output if finished."""
    proc_info = BG_PROCESS_STORE.get(process_id)
    if proc_info is None:
        return None
    proc = proc_info.get("_subprocess")
    if proc is not None and proc.poll() is not None:
        # Process has finished
        proc_info["running"] = False
        proc_info["finished_at"] = time.time()
        proc_info["returncode"] = proc.returncode
        proc_info["stdout"] = (proc.stdout.read() if proc.stdout else "")[:COMMAND_OUTPUT_MAX_BYTES]
        proc_info["stderr"] = (proc.stderr.read() if proc.stderr else "")[:COMMAND_OUTPUT_MAX_BYTES]
        proc_info["_subprocess"] = None
    return proc_info


def list_bg_processes() -> List[Dict[str, Any]]:
    """List all background processes."""
    _cleanup_expired_processes()
    result = []
    for pid, proc_info in list(BG_PROCESS_STORE.items()):
        info = get_bg_process(pid)
        if info is not None:
            result.append(info)
    return result


def stop_bg_process(process_id: str) -> Dict[str, Any]:
    """Stop a running background process."""
    proc_info = BG_PROCESS_STORE.get(process_id)
    if proc_info is None:
        return {"error": "process not found"}
    if not proc_info.get("running"):
        return {"error": "process is not running"}
    proc = proc_info.get("_subprocess")
    if proc is None:
        return {"error": "process handle lost"}
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    except Exception as exc:
        return {"error": f"failed to stop process: {exc}"}
    proc_info["running"] = False
    proc_info["finished_at"] = time.time()
    proc_info["returncode"] = proc.returncode
    proc_info["stdout"] = (proc.stdout.read() if proc.stdout else "")[:COMMAND_OUTPUT_MAX_BYTES]
    proc_info["stderr"] = (proc.stderr.read() if proc.stderr else "")[:COMMAND_OUTPUT_MAX_BYTES]
    proc_info["_subprocess"] = None
    return {"process_id": process_id, "stopped": True, "returncode": proc.returncode}


# Tools that execute external commands (logged without args to avoid secrets)
EXEC_TOOLS = frozenset({
    "exec_command", "shell_safe", "python",
    "web_search", "browser_search", "browser_open", "browser_extract",
    "http_request", "sqlite_query",
    "start_bg_process", "bg_process_status", "list_bg_processes", "stop_bg_process",
})

# Tools that read/write filesystem inside workspace (logged with paths only)
FS_TOOLS = frozenset({
    "list_files", "read_file", "write_file", "patch_file", "mkdir",
    "move_file", "delete_file", "file_stat",
})


def sanitize_for_logging(args: Dict[str, Any], tool_name: str) -> Dict[str, Any]:
    """Return a copy of args safe to log: strip secrets from exec commands."""
    if tool_name not in EXEC_TOOLS:
        return args
    sanitized = {}
    secret_pattern = re.compile(
        r"(api[_-]?key|password|token|secret|auth|authorization|bearer)",
        re.IGNORECASE,
    )
    for k, v in args.items():
        if secret_pattern.search(str(k)):
            sanitized[k] = "[REDACTED]"
        elif isinstance(v, str) and secret_pattern.search(v):
            sanitized[k] = "[REDACTED]"
        else:
            sanitized[k] = v
    return sanitized


def log_tool_call(tool_name: str, args: Dict[str, Any], result: Any, duration_ms: float, request_id: Optional[str] = None) -> None:
    """Structured log for a tool call: name, safe args, status, duration_ms, request_id."""
    safe_args = sanitize_for_logging(args, tool_name)
    status = "ok" if isinstance(result, dict) and "error" not in result else "error"
    extra = f" request_id={request_id}" if request_id else ""
    logger.info(
        "tool_call name=%s args=%s status=%s duration_ms=%.2f%s",
        tool_name, safe_args, status, duration_ms, extra,
    )

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
    # Separate counters for tool categories
    "tool_exec_calls": 0,
    "tool_fs_calls": 0,
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
