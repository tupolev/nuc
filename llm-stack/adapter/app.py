from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import asyncio
import calendar
import heapq
import html
import httpx
import json
import os
import re
import shlex
import sqlite3
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

from auth_security import (
    API_KEY_SALT_ENV,
    API_KEY_SECRET_ENV,
    derive_api_key_hash,
    ensure_api_keys_schema,
    get_required_env,
    migrate_legacy_plaintext_keys,
)

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
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_MAX_BYTES = int(os.getenv("HTTP_MAX_BYTES", "300000"))
SQLITE_QUERY_MAX_ROWS = int(os.getenv("SQLITE_QUERY_MAX_ROWS", "200"))
SHELL_TIMEOUT = int(os.getenv("SHELL_TIMEOUT", "20"))
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "qwen2.5-coder:7b")
TOOL_EXECUTION_MODE = os.getenv("TOOL_EXECUTION_MODE", "server").strip().lower()
AUTO_ENABLE_LOCAL_TOOLS = os.getenv("AUTO_ENABLE_LOCAL_TOOLS", "false").strip().lower() == "true"
SAFE_SHELL_COMMANDS = {
    item.strip()
    for item in os.getenv(
        "SAFE_SHELL_COMMANDS",
        "date,uname,whoami,uptime,df,free,ls,pwd,cat,head,tail,sed,rg,find,stat,du",
    ).split(",")
    if item.strip()
}

PRIORITY_MAP = {"high": 0, "medium": 1, "low": 2}

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


async def run_web_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}
    provider = str(args.get("provider", "auto")).strip().lower() or "auto"
    max_results = max(1, min(int(args.get("max_results", 5)), 10))
    providers = [provider] if provider != "auto" else ["google", "duckduckgo"]
    attempted_providers: List[str] = []

    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        last_error = None
        for current_provider in providers:
            attempted_providers.append(current_provider)
            try:
                if current_provider == "google":
                    response = await client.get(
                        f"https://www.google.com/search?q={quote_plus(query)}&hl=en",
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    response.raise_for_status()
                    results = parse_google_results(response.text, max_results)
                elif current_provider in {"duckduckgo", "ddg"}:
                    response = await client.get(
                        "https://duckduckgo.com/html/",
                        params={"q": query},
                        headers={"User-Agent": "Mozilla/5.0"},
                    )
                    response.raise_for_status()
                    results = parse_duckduckgo_results(response.text, max_results)
                    current_provider = "duckduckgo"
                else:
                    return {"error": "provider must be one of: auto, google, duckduckgo"}

                if results:
                    return {
                        "query": query,
                        "provider": current_provider,
                        "attempted_providers": attempted_providers,
                        "results": results,
                    }
            except Exception as exc:
                last_error = str(exc)

    return {
        "error": "search failed or returned no results",
        "query": query,
        "provider": provider,
        "attempted_providers": attempted_providers,
        "details": last_error,
    }


def parse_google_results(raw_html: str, max_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    pattern = re.compile(r'<a href="/url\?q=(https?://[^"&]+)[^"]*".*?<h3[^>]*>(.*?)</h3>', re.S)
    snippets = re.findall(r'<div[^>]*data-sncf="1"[^>]*>(.*?)</div>', raw_html, re.S)
    snippet_index = 0

    for match in pattern.finditer(raw_html):
        url = html.unescape(match.group(1))
        title = clean_html_fragment(match.group(2))
        if not title or url.startswith("https://support.google.com"):
            continue

        snippet = ""
        while snippet_index < len(snippets):
            snippet = clean_html_fragment(snippets[snippet_index])
            snippet_index += 1
            if snippet:
                break

        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results


def parse_duckduckgo_results(raw_html: str, max_results: int) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    blocks = re.findall(r'<div class="result(?: results_links[^"]*)?".*?</div>\s*</div>', raw_html, re.S)
    if not blocks:
        blocks = re.findall(r'<div class="result__body".*?</div>\s*</div>', raw_html, re.S)

    for block in blocks:
        link_match = re.search(r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link_match:
            continue
        url = normalize_duckduckgo_url(html.unescape(link_match.group(1)))
        title = clean_html_fragment(link_match.group(2))
        snippet_match = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        if not snippet_match:
            snippet_match = re.search(r'<div[^>]+class="result__snippet"[^>]*>(.*?)</div>', block, re.S)
        snippet = clean_html_fragment(snippet_match.group(1)) if snippet_match else ""
        results.append({"title": title, "url": url, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results


def normalize_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return url


def clean_html_fragment(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


async def run_fetch_url(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "url is required"}
    if not (url.startswith("http://") or url.startswith("https://")):
        return {"error": "url must start with http:// or https://"}

    mode = str(args.get("mode", "text")).strip().lower() or "text"
    timeout_seconds = max(3, min(int(args.get("timeout_seconds", 20)), 60))

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if mode == "json":
        try:
            payload = response.json()
        except Exception as exc:
            return {"error": f"response is not valid JSON: {exc}", "url": url, "content_type": content_type}
        return {"url": url, "content_type": content_type, "status_code": response.status_code, "json": payload}

    text = response.text
    if mode == "html_text":
        text = clean_html_fragment(text)

    return {
        "url": url,
        "content_type": content_type,
        "status_code": response.status_code,
        "text": text[:TOOL_OUTPUT_MAX_LEN],
    }


async def run_weather(args: Dict[str, Any]) -> Dict[str, Any]:
    location = str(args.get("location", "")).strip()
    if not location:
        return {"error": "location is required"}

    encoded_location = quote_plus(location)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        response = await client.get(
            f"https://wttr.in/{encoded_location}",
            params={"format": "j1"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()

    data = response.json()
    current = (data.get("current_condition") or [{}])[0]
    nearest = (data.get("nearest_area") or [{}])[0]
    weather_days = data.get("weather") or []

    forecast = []
    for day in weather_days[:3]:
        hourly = (day.get("hourly") or [{}])[0]
        forecast.append(
            {
                "date": day.get("date"),
                "max_temp_c": day.get("maxtempC"),
                "min_temp_c": day.get("mintempC"),
                "description": ((hourly.get("weatherDesc") or [{}])[0]).get("value"),
            }
        )

    return {
        "location": location,
        "resolved_area": {
            "area": nearest.get("areaName", [{}])[0].get("value"),
            "region": nearest.get("region", [{}])[0].get("value"),
            "country": nearest.get("country", [{}])[0].get("value"),
        },
        "current": {
            "temp_c": current.get("temp_C"),
            "feels_like_c": current.get("FeelsLikeC"),
            "humidity": current.get("humidity"),
            "wind_kmph": current.get("windspeedKmph"),
            "description": ((current.get("weatherDesc") or [{}])[0]).get("value"),
        },
        "forecast": forecast,
        "source": "wttr.in",
    }


async def run_time_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    tz_name = str(args.get("timezone", "")).strip()
    utc_offset = str(args.get("utc_offset", "")).strip()

    if tz_name:
        if ZoneInfo is None:
            return {"error": "timezone support is unavailable on this Python build"}
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            return {"error": f"invalid timezone: {tz_name}"}
        now = datetime.now(tz)
        return {
            "timezone": tz_name,
            "iso": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
            "utc_offset": now.strftime("%z"),
        }

    if utc_offset:
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", utc_offset)
        if not match:
            return {"error": "utc_offset must look like +02:00 or -05:30"}
        sign, hours, minutes = match.groups()
        total_minutes = int(hours) * 60 + int(minutes)
        if sign == "-":
            total_minutes *= -1
        tz = timezone(timedelta(minutes=total_minutes))
        now = datetime.now(tz)
        return {
            "utc_offset": utc_offset,
            "iso": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
        }

    now = datetime.now(timezone.utc)
    return {
        "timezone": "UTC",
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "utc_offset": "+0000",
    }


async def run_calendar_tool(args: Dict[str, Any]) -> Dict[str, Any]:
    year = args.get("year")
    month = args.get("month")
    date_str = str(args.get("date", "")).strip()

    if date_str:
        try:
            dt = datetime.fromisoformat(date_str)
        except Exception:
            return {"error": "date must be ISO format like 2026-04-03 or 2026-04-03T15:30:00"}
        return {
            "date": dt.date().isoformat(),
            "weekday": dt.strftime("%A"),
            "iso_week": dt.isocalendar().week,
            "month": dt.month,
            "year": dt.year,
        }

    now = datetime.now()
    year = int(year if year is not None else now.year)
    month = int(month if month is not None else now.month)
    if month < 1 or month > 12:
        return {"error": "month must be between 1 and 12"}

    cal = calendar.TextCalendar(firstweekday=0)
    month_text = cal.formatmonth(year, month)
    month_meta = calendar.monthrange(year, month)
    return {
        "year": year,
        "month": month,
        "month_name": calendar.month_name[month],
        "first_weekday_index": month_meta[0],
        "days_in_month": month_meta[1],
        "rendered_month": month_text,
    }


async def run_news_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}

    max_results = max(1, min(int(args.get("max_results", 5)), 20))
    language = str(args.get("language", "en")).strip() or "en"
    country = str(args.get("country", "US")).strip().upper() or "US"

    params = urlencode(
        {
            "q": query,
            "hl": f"{language}-{country}",
            "gl": country,
            "ceid": f"{country}:{language}",
        }
    )
    url = f"https://news.google.com/rss/search?{params}"

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()

    root = ET.fromstring(response.text)
    items = []
    for item in root.findall("./channel/item")[:max_results]:
        items.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "link": (item.findtext("link") or "").strip(),
                "published": (item.findtext("pubDate") or "").strip(),
                "source": (item.findtext("source") or "").strip(),
            }
        )

    return {
        "query": query,
        "provider": "google_news_rss",
        "results": items,
    }


async def run_geocode(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    lat = args.get("lat")
    lon = args.get("lon")
    max_results = max(1, min(int(args.get("max_results", 5)), 10))

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        if query:
            response = await client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": str(max_results),
                },
                headers={"User-Agent": "llm-stack-adapter/1.0"},
            )
            response.raise_for_status()
            data = response.json()
            return {
                "query": query,
                "provider": "nominatim",
                "results": [
                    {
                        "display_name": row.get("display_name"),
                        "lat": row.get("lat"),
                        "lon": row.get("lon"),
                        "type": row.get("type"),
                        "importance": row.get("importance"),
                    }
                    for row in data
                ],
            }

        if lat is None or lon is None:
            return {"error": "provide either query or both lat and lon"}

        response = await client.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={
                "lat": str(lat),
                "lon": str(lon),
                "format": "jsonv2",
            },
            headers={"User-Agent": "llm-stack-adapter/1.0"},
        )
        response.raise_for_status()
        data = response.json()
        return {
            "provider": "nominatim",
            "result": {
                "display_name": data.get("display_name"),
                "lat": data.get("lat"),
                "lon": data.get("lon"),
                "address": data.get("address"),
            },
        }


async def run_http_request(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "url is required"}
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}

    method = str(args.get("method", "GET")).strip().upper() or "GET"
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
        return {"error": "unsupported method"}

    headers = args.get("headers", {})
    if headers and not isinstance(headers, dict):
        return {"error": "headers must be an object"}

    mode = str(args.get("mode", "text")).strip().lower() or "text"
    timeout_seconds = max(3, min(int(args.get("timeout_seconds", HTTP_TIMEOUT)), 90))
    body = args.get("body")

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.request(
            method,
            url,
            headers=headers,
            json=body if isinstance(body, (dict, list)) else None,
            content=body if isinstance(body, str) else None,
        )
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if mode == "json":
        try:
            payload = response.json()
        except Exception as exc:
            return {"error": f"response is not valid JSON: {exc}", "url": url, "content_type": content_type}
        return {"url": url, "method": method, "status_code": response.status_code, "content_type": content_type, "json": payload}

    text = response.text[:HTTP_MAX_BYTES]
    if mode == "html_text":
        text = clean_html_fragment(text)
    return {"url": url, "method": method, "status_code": response.status_code, "content_type": content_type, "text": text[:TOOL_OUTPUT_MAX_LEN]}


async def run_sqlite_query(args: Dict[str, Any]) -> Dict[str, Any]:
    db_path = str(args.get("db_path", AUTH_DB_PATH)).strip() or AUTH_DB_PATH
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}

    lowered = query.lower()
    if ";" in query.strip().rstrip(";"):
        return {"error": "multiple SQL statements are not allowed"}

    readonly = str(args.get("readonly", "true")).strip().lower() != "false"
    if readonly and not lowered.startswith(("select", "pragma", "with", "explain")):
        return {"error": "readonly mode only allows select/pragma/with/explain queries"}

    conn_local = sqlite3.connect(db_path)
    conn_local.row_factory = sqlite3.Row
    try:
        cur = conn_local.execute(query)
        rows = cur.fetchmany(SQLITE_QUERY_MAX_ROWS)
        columns = [desc[0] for desc in (cur.description or [])]
        return {
            "db_path": db_path,
            "readonly": readonly,
            "columns": columns,
            "rows": [dict(row) for row in rows],
            "row_count": len(rows),
        }
    finally:
        conn_local.close()


async def run_shell_safe(args: Dict[str, Any]) -> Dict[str, Any]:
    command = str(args.get("command", "")).strip()
    if not command:
        return {"error": "command is required"}

    try:
        parts = shlex.split(command)
    except Exception as exc:
        return {"error": f"invalid shell command: {exc}"}

    if not parts:
        return {"error": "command is empty"}

    executable = parts[0]
    if executable not in SAFE_SHELL_COMMANDS:
        return {
            "error": "command is not in SAFE_SHELL_COMMANDS allowlist",
            "allowed_commands": sorted(SAFE_SHELL_COMMANDS),
        }

    workdir = str(args.get("workdir", "/tmp")).strip() or "/tmp"
    timeout_seconds = max(1, min(int(args.get("timeout_seconds", SHELL_TIMEOUT)), 60))

    result = subprocess.run(
        parts,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    return {
        "command": parts,
        "workdir": workdir,
        "returncode": result.returncode,
        "stdout": result.stdout[:TOOL_OUTPUT_MAX_LEN],
        "stderr": result.stderr[:TOOL_OUTPUT_MAX_LEN],
    }


async def run_calendar_events(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    path = str(args.get("path", "")).strip()
    start_date = str(args.get("start_date", "")).strip()
    end_date = str(args.get("end_date", "")).strip()
    max_results = max(1, min(int(args.get("max_results", 20)), 100))

    if not url and not path:
        return {"error": "provide either url or path"}

    if url:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        raw_ics = response.text
    else:
        with open(path, "r", encoding="utf-8") as handle:
            raw_ics = handle.read()

    events = parse_ics_events(raw_ics)
    start_filter = datetime.fromisoformat(start_date).date() if start_date else None
    end_filter = datetime.fromisoformat(end_date).date() if end_date else None

    filtered = []
    for event in events:
        event_date = event.get("start_date")
        if event_date:
            event_date_obj = datetime.fromisoformat(event_date).date()
            if start_filter and event_date_obj < start_filter:
                continue
            if end_filter and event_date_obj > end_filter:
                continue
        filtered.append(event)
        if len(filtered) >= max_results:
            break

    return {
        "source": url or path,
        "events": filtered,
        "event_count": len(filtered),
    }


def parse_ics_events(raw_ics: str) -> List[Dict[str, Any]]:
    unfolded_lines = []
    for line in raw_ics.splitlines():
        if line.startswith((" ", "\t")) and unfolded_lines:
            unfolded_lines[-1] += line[1:]
        else:
            unfolded_lines.append(line)

    events = []
    current: Optional[Dict[str, Any]] = None
    for line in unfolded_lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(normalize_ics_event(current))
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key] = value
    return events


def normalize_ics_event(event: Dict[str, Any]) -> Dict[str, Any]:
    summary = event.get("SUMMARY", "")
    location = event.get("LOCATION", "")
    description = event.get("DESCRIPTION", "")
    dtstart_key = next((key for key in event.keys() if key.startswith("DTSTART")), "")
    dtend_key = next((key for key in event.keys() if key.startswith("DTEND")), "")
    start_value = event.get(dtstart_key, "")
    end_value = event.get(dtend_key, "")
    return {
        "summary": summary,
        "location": location,
        "description": description[:1000],
        "start": start_value,
        "end": end_value,
        "start_date": normalize_ics_date(start_value),
        "end_date": normalize_ics_date(end_value),
    }


def normalize_ics_date(value: str) -> Optional[str]:
    if not value:
        return None
    cleaned = value.rstrip("Z")
    for fmt in ("%Y%m%d", "%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.strptime(cleaned, fmt).isoformat()
        except Exception:
            pass
    return value


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
        "description": "Search the web using Google or DuckDuckGo and return structured results. Use this when fresh web information is needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to submit."},
                "provider": {
                    "type": "string",
                    "description": "Search provider: auto, google, or duckduckgo.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of search results to return, between 1 and 10.",
                },
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
    "fetch_url": {
        "description": "Fetch a URL and return text or JSON content. Use this for reading specific pages or APIs after you already know the URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http or https URL to fetch."},
                "mode": {
                    "type": "string",
                    "description": "How to interpret the response: text, html_text, or json.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Request timeout in seconds.",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": run_fetch_url,
    },
    "weather": {
        "description": "Fetch current weather and a short forecast for a location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "description": "Location name such as Madrid or San Francisco."}
            },
            "required": ["location"],
            "additionalProperties": False,
        },
        "handler": run_weather,
    },
    "time": {
        "description": "Get the current time for a timezone or UTC offset.",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {"type": "string", "description": "IANA timezone name, for example Europe/Madrid."},
                "utc_offset": {"type": "string", "description": "UTC offset like +02:00 or -05:00."},
            },
            "additionalProperties": False,
        },
        "handler": run_time_tool,
    },
    "calendar": {
        "description": "Inspect calendar information for a date or render a month calendar.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "ISO date or datetime, for example 2026-04-03."},
                "year": {"type": "integer", "description": "Year for month rendering."},
                "month": {"type": "integer", "description": "Month for month rendering, 1 to 12."},
            },
            "additionalProperties": False,
        },
        "handler": run_calendar_tool,
    },
    "news_search": {
        "description": "Search recent news using Google News RSS and return structured results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "News search query."},
                "max_results": {"type": "integer", "description": "Maximum number of results to return."},
                "language": {"type": "string", "description": "Language code like en or es."},
                "country": {"type": "string", "description": "Country code like US or ES."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": run_news_search,
    },
    "geocode": {
        "description": "Geocode a place name or reverse-geocode coordinates using OpenStreetMap Nominatim.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Place name to geocode."},
                "lat": {"type": "string", "description": "Latitude for reverse geocoding."},
                "lon": {"type": "string", "description": "Longitude for reverse geocoding."},
                "max_results": {"type": "integer", "description": "Maximum number of geocoding results."},
            },
            "additionalProperties": False,
        },
        "handler": run_geocode,
    },
    "http_request": {
        "description": "Make a general HTTP request and return JSON or text. Use this for APIs or known URLs that need more than a simple GET.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http or https URL."},
                "method": {"type": "string", "description": "HTTP method: GET, POST, PUT, PATCH, DELETE, or HEAD."},
                "headers": {"type": "object", "description": "HTTP headers as an object."},
                "body": {"description": "Request body as a string or JSON object."},
                "mode": {"type": "string", "description": "Response mode: text, html_text, or json."},
                "timeout_seconds": {"type": "integer", "description": "Request timeout in seconds."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": run_http_request,
    },
    "sqlite_query": {
        "description": "Run a SQLite query against a local database file. Default mode is readonly.",
        "parameters": {
            "type": "object",
            "properties": {
                "db_path": {"type": "string", "description": "Path to the SQLite database file."},
                "query": {"type": "string", "description": "SQL query to execute."},
                "readonly": {"type": "string", "description": "Set to false to allow non-readonly queries."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": run_sqlite_query,
    },
    "shell_safe": {
        "description": "Run an allowlisted shell command without a shell. Good for safe local inspection tasks.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to run, parsed with shlex."},
                "workdir": {"type": "string", "description": "Working directory."},
                "timeout_seconds": {"type": "integer", "description": "Timeout in seconds."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "handler": run_shell_safe,
    },
    "calendar_events": {
        "description": "Read real calendar events from an ICS URL or local ICS file and filter them by date range.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "ICS URL to read."},
                "path": {"type": "string", "description": "Local path to an ICS file."},
                "start_date": {"type": "string", "description": "ISO start date filter."},
                "end_date": {"type": "string", "description": "ISO end date filter."},
                "max_results": {"type": "integer", "description": "Maximum number of events to return."},
            },
            "additionalProperties": False,
        },
        "handler": run_calendar_events,
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
        expected_type = definition.get("type")
        value = args[field]
        if expected_type == "string" and not isinstance(value, str):
            raise ValueError(f"field '{field}' must be a string")
        if expected_type == "integer" and isinstance(value, bool):
            raise ValueError(f"field '{field}' must be an integer")
        if expected_type == "integer" and not isinstance(value, int):
            raise ValueError(f"field '{field}' must be an integer")


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
    get_required_env(API_KEY_SECRET_ENV)
    get_required_env(API_KEY_SALT_ENV)
    migrate_legacy_plaintext_keys(conn)
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
