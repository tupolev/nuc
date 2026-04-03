import calendar
import html
import json
import os
import re
import shlex
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse

import httpx

from config import (
    AUTH_DB_PATH,
    FILES_DIR,
    HTTP_MAX_BYTES,
    HTTP_TIMEOUT,
    PYTHON_CODE_MAX_LEN,
    PYTHON_TIMEOUT,
    SAFE_SHELL_COMMANDS,
    SHELL_TIMEOUT,
    SQLITE_QUERY_MAX_ROWS,
    TOOL_ARG_MAX_LEN,
    TOOL_OUTPUT_MAX_LEN,
)
from state import METRICS, bump_metric_dict, bump_tool_status_metric

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


def clean_html_fragment(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


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


def normalize_duckduckgo_url(url: str) -> str:
    if url.startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    uddg = query.get("uddg")
    if uddg:
        return unquote(uddg[0])
    return url


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
        return {
            "url": url,
            "method": method,
            "status_code": response.status_code,
            "content_type": content_type,
            "json": payload,
        }

    text = response.text[:HTTP_MAX_BYTES]
    if mode == "html_text":
        text = clean_html_fragment(text)
    return {
        "url": url,
        "method": method,
        "status_code": response.status_code,
        "content_type": content_type,
        "text": text[:TOOL_OUTPUT_MAX_LEN],
    }


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
                "provider": {"type": "string", "description": "Search provider: auto, google, or duckduckgo."},
                "max_results": {"type": "integer", "description": "Maximum number of search results to return, between 1 and 10."},
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
            "properties": {"code": {"type": "string", "description": "Python code to execute."}},
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
                "filename": {"type": "string", "description": "Base file name, for example notes.txt."},
                "content": {"type": "string", "description": "Full UTF-8 file content."},
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
                "mode": {"type": "string", "description": "How to interpret the response: text, html_text, or json."},
                "timeout_seconds": {"type": "integer", "description": "Request timeout in seconds."},
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


async def execute_tool_call(name: str, raw_args: Any) -> Dict[str, Any]:
    METRICS["tool_requests_total"] += 1
    start = datetime.now().timestamp()

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
        METRICS["tool_latency"].append(datetime.now().timestamp() - start)
