import calendar
import html
import json
import os
import pathlib
import re
import shlex
import shutil
import sqlite3
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse

import httpx

from config import (
    AUTH_DB_PATH,
    COMMAND_OUTPUT_MAX_BYTES,
    FILES_DIR,
    FILE_READ_MAX_BYTES,
    FILE_WRITE_MAX_BYTES,
    HTTP_MAX_BYTES,
    HTTP_TIMEOUT,
    PATCH_MAX_BYTES,
    PYTHON_CODE_MAX_LEN,
    PYTHON_TIMEOUT,
    EXEC_COMMAND_ALLOWLIST,
    SAFE_SHELL_COMMANDS,
    SHELL_TIMEOUT,
    SQLITE_QUERY_MAX_ROWS,
    TOOL_ARG_MAX_LEN,
    TOOL_OUTPUT_MAX_LEN,
    WORKSPACE_DIR,
)
from state import (
    BG_PROCESS_STORE,
    EXEC_TOOLS,
    FS_TOOLS,
    METRICS,
    append_tool_call,
    bump_metric_dict,
    bump_tool_status_metric,
    get_bg_process,
    list_bg_processes,
    log_tool_call,
    SESSION_STORE,
    start_bg_process,
    stop_bg_process,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None


WORKSPACE_ROOT = pathlib.Path(WORKSPACE_DIR).resolve()


def ensure_workspace_root() -> None:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)


def resolve_workspace_path(raw_path: str, allow_missing: bool = True) -> pathlib.Path:
    ensure_workspace_root()
    candidate = str(raw_path or ".").strip() or "."
    path_obj = pathlib.Path(candidate)
    if path_obj.is_absolute():
        raise ValueError("path must be relative to the workspace root")

    resolved = (WORKSPACE_ROOT / path_obj).resolve(strict=False)
    workspace_root_str = str(WORKSPACE_ROOT)
    resolved_str = str(resolved)
    if os.path.commonpath([workspace_root_str, resolved_str]) != workspace_root_str:
        raise ValueError("path escapes the workspace root")

    if not allow_missing and not resolved.exists():
        raise FileNotFoundError(f"path not found: {candidate}")

    return resolved


def workspace_relpath(path: pathlib.Path) -> str:
    relative = path.relative_to(WORKSPACE_ROOT)
    value = str(relative)
    return "." if not value else value


def validate_utf8_size(content: str, max_bytes: int, label: str) -> bytes:
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValueError(f"{label} exceeds max size of {max_bytes} bytes")
    return encoded


def clean_html_fragment(fragment: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", fragment, flags=re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def clean_browser_text(fragment: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg|canvas).*?>.*?</\1>", " ", fragment)
    text = re.sub(r"(?is)<(header|footer|nav|aside|form).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?is)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?is)</(p|div|section|article|main|li|h1|h2|h3|h4|h5|h6|tr)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\r", "", text)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_html_title(raw_html: str) -> str:
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw_html)
    return clean_html_fragment(match.group(1)) if match else ""


def extract_meta_description(raw_html: str) -> str:
    patterns = [
        r'(?is)<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']',
        r'(?is)<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]+property=["\']og:description["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, raw_html)
        if match:
            return clean_html_fragment(match.group(1))
    return ""


def extract_headings(raw_html: str, max_headings: int = 10) -> List[Dict[str, str]]:
    headings: List[Dict[str, str]] = []
    for match in re.finditer(r"(?is)<(h[1-6])[^>]*>(.*?)</\1>", raw_html):
        text = clean_html_fragment(match.group(2))
        if not text:
            continue
        headings.append({"level": match.group(1), "text": text})
        if len(headings) >= max_headings:
            break
    return headings


def extract_links(raw_html: str, base_url: str, max_links: int = 20) -> List[Dict[str, str]]:
    seen: set[str] = set()
    links: List[Dict[str, str]] = []
    for match in re.finditer(r'(?is)<a[^>]+href=["\'](.*?)["\'][^>]*>(.*?)</a>', raw_html):
        href = html.unescape(match.group(1)).strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        absolute = str(httpx.URL(base_url).join(href))
        if absolute in seen:
            continue
        seen.add(absolute)
        text = clean_html_fragment(match.group(2))
        links.append({"url": absolute, "text": text})
        if len(links) >= max_links:
            break
    return links


def extract_main_content_block(raw_html: str) -> str:
    candidate_patterns = [
        r"(?is)<main[^>]*>(.*?)</main>",
        r"(?is)<article[^>]*>(.*?)</article>",
        r'(?is)<div[^>]+(?:id|class)=["\'][^"\']*(?:content|article|main|post|docs|documentation|markdown-body)[^"\']*["\'][^>]*>(.*?)</div>',
        r'(?is)<section[^>]+(?:id|class)=["\'][^"\']*(?:content|article|main|post|docs|documentation)[^"\']*["\'][^>]*>(.*?)</section>',
    ]
    candidates: List[str] = []
    for pattern in candidate_patterns:
        candidates.extend(match.group(1) for match in re.finditer(pattern, raw_html))
    if not candidates:
        return raw_html
    return max(candidates, key=lambda item: len(clean_browser_text(item)))


def build_browser_page_payload(
    *,
    url: str,
    final_url: str,
    raw_html: str,
    status_code: int,
    content_type: str,
    max_links: int,
    text_max_chars: int,
) -> Dict[str, Any]:
    main_block = extract_main_content_block(raw_html)
    main_text = clean_browser_text(main_block)[:text_max_chars]
    page_text = clean_browser_text(raw_html)[:text_max_chars]
    return {
        "url": url,
        "final_url": final_url,
        "status_code": status_code,
        "content_type": content_type,
        "title": extract_html_title(raw_html),
        "description": extract_meta_description(raw_html),
        "headings": extract_headings(raw_html),
        "links": extract_links(raw_html, final_url, max_links=max_links),
        "main_text": main_text,
        "page_text": page_text,
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


async def run_browser_search(args: Dict[str, Any]) -> Dict[str, Any]:
    query = str(args.get("query", "")).strip()
    if not query:
        return {"error": "query is required"}

    domains = args.get("domains", [])
    if domains and not isinstance(domains, list):
        return {"error": "domains must be an array of hostnames"}

    domain_filters = [str(item).strip() for item in domains if str(item).strip()]
    scoped_query = query
    if domain_filters:
        scoped_query += " " + " OR ".join(f"site:{domain}" for domain in domain_filters)

    payload = await run_web_search(
        {
            "query": scoped_query,
            "provider": args.get("provider", "auto"),
            "max_results": args.get("max_results", 5),
        }
    )
    if payload.get("error"):
        return payload

    return {
        "query": query,
        "scoped_query": scoped_query,
        "domains": domain_filters,
        "provider": payload.get("provider"),
        "attempted_providers": payload.get("attempted_providers", []),
        "results": payload.get("results", []),
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


async def run_browser_open(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "url is required"}
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}

    timeout_seconds = max(3, min(int(args.get("timeout_seconds", HTTP_TIMEOUT)), 60))
    max_links = max(0, min(int(args.get("max_links", 20)), 100))
    text_max_chars = max(500, min(int(args.get("max_chars", TOOL_OUTPUT_MAX_LEN)), TOOL_OUTPUT_MAX_LEN))

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type.lower():
        return {
            "url": url,
            "final_url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "text": response.text[:text_max_chars],
        }

    payload = build_browser_page_payload(
        url=url,
        final_url=str(response.url),
        raw_html=response.text,
        status_code=response.status_code,
        content_type=content_type,
        max_links=max_links,
        text_max_chars=text_max_chars,
    )
    return payload


async def run_browser_extract(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "url is required"}
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}

    strategy = str(args.get("strategy", "main")).strip().lower() or "main"
    if strategy not in {"main", "full"}:
        return {"error": "strategy must be one of: main, full"}

    timeout_seconds = max(3, min(int(args.get("timeout_seconds", HTTP_TIMEOUT)), 60))
    text_max_chars = max(500, min(int(args.get("max_chars", TOOL_OUTPUT_MAX_LEN)), TOOL_OUTPUT_MAX_LEN))

    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client:
        response = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        response.raise_for_status()

    content_type = response.headers.get("content-type", "")
    raw_html = response.text
    if "html" not in content_type.lower():
        return {
            "url": url,
            "final_url": str(response.url),
            "status_code": response.status_code,
            "content_type": content_type,
            "strategy": strategy,
            "text": raw_html[:text_max_chars],
        }

    main_block = extract_main_content_block(raw_html)
    text = clean_browser_text(main_block if strategy == "main" else raw_html)[:text_max_chars]
    return {
        "url": url,
        "final_url": str(response.url),
        "status_code": response.status_code,
        "content_type": content_type,
        "strategy": strategy,
        "title": extract_html_title(raw_html),
        "description": extract_meta_description(raw_html),
        "headings": extract_headings(raw_html),
        "text": text,
    }


async def run_browser_screenshot(args: Dict[str, Any]) -> Dict[str, Any]:
    url = str(args.get("url", "")).strip()
    if not url:
        return {"error": "url is required"}
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}

    timeout_seconds = max(5, min(int(args.get("timeout_seconds", HTTP_TIMEOUT)), 60))
    viewport_width = max(320, min(int(args.get("viewport_width", 1280)), 3840))
    viewport_height = max(240, min(int(args.get("viewport_height", 800)), 2160))
    full_page = str(args.get("full_page", "false")).strip().lower() == "true"

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"error": "browser_screenshot requires playwright to be installed in the container"}

    screenshot_bytes: Optional[bytes] = None
    error_msg = ""

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            page = await browser.new_page(
                viewport={"width": viewport_width, "height": viewport_height},
            )
            await page.goto(url, timeout=timeout_seconds * 1000, wait_until="networkidle")
            screenshot_bytes = await page.screenshot(
                full_page=full_page,
                type="png",
            )
            await browser.close()
    except Exception as exc:
        error_msg = str(exc)

    if screenshot_bytes is None:
        return {"error": f"screenshot failed: {error_msg}"}

    import base64
    b64 = base64.b64encode(screenshot_bytes).decode("utf-8")
    return {
        "url": url,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "full_page": full_page,
        "format": "png",
        "size_bytes": len(screenshot_bytes),
        "base64": b64[:TOOL_OUTPUT_MAX_LEN],
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
    return run_sqlite_query_sync(args)


def run_sqlite_query_sync(args: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous version of run_sqlite_query for testing."""
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


async def run_list_files(args: Dict[str, Any]) -> Dict[str, Any]:
    base_path = resolve_workspace_path(str(args.get("path", ".")).strip(), allow_missing=False)
    recursive = bool(args.get("recursive", False))
    max_entries = max(1, min(int(args.get("max_entries", 200)), 2000))

    if not base_path.is_dir():
        return {"error": "path must point to a directory"}

    entries: List[Dict[str, Any]] = []

    def append_entry(item: pathlib.Path) -> None:
        stat_result = item.stat()
        entries.append(
            {
                "path": workspace_relpath(item),
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "size": stat_result.st_size,
            }
        )

    if recursive:
        for root, dirnames, filenames in os.walk(base_path):
            dirnames.sort()
            filenames.sort()
            root_path = pathlib.Path(root)
            for dirname in dirnames:
                if len(entries) >= max_entries:
                    break
                append_entry(root_path / dirname)
            for filename in filenames:
                if len(entries) >= max_entries:
                    break
                append_entry(root_path / filename)
            if len(entries) >= max_entries:
                break
    else:
        for item in sorted(base_path.iterdir(), key=lambda entry: entry.name):
            if len(entries) >= max_entries:
                break
            append_entry(item)

    return {
        "path": workspace_relpath(base_path),
        "recursive": recursive,
        "entries": entries,
        "truncated": len(entries) >= max_entries,
    }


async def run_read_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = resolve_workspace_path(str(args.get("path", "")).strip(), allow_missing=False)
    start_line = args.get("start_line")
    end_line = args.get("end_line")

    if not path.is_file():
        return {"error": "path must point to a file"}

    raw_bytes = path.read_bytes()
    if len(raw_bytes) > FILE_READ_MAX_BYTES:
        return {"error": f"file exceeds max readable size of {FILE_READ_MAX_BYTES} bytes"}

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": "file is not valid UTF-8"}

    if start_line is None and end_line is None:
        return {
            "path": workspace_relpath(path),
            "content": text,
            "line_count": len(text.splitlines()),
        }

    lines = text.splitlines()
    start_index = 1 if start_line is None else max(1, int(start_line))
    end_index = len(lines) if end_line is None else min(len(lines), int(end_line))
    if end_index < start_index:
        return {"error": "end_line must be greater than or equal to start_line"}

    selected = lines[start_index - 1:end_index]
    return {
        "path": workspace_relpath(path),
        "content": "\n".join(selected),
        "start_line": start_index,
        "end_line": end_index,
        "line_count": len(lines),
    }


async def run_write_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = resolve_workspace_path(str(args.get("path", "")).strip(), allow_missing=True)
    content = str(args.get("content", ""))
    create_dirs = bool(args.get("create_dirs", False))
    overwrite = bool(args.get("overwrite", True))
    encoded = validate_utf8_size(content, FILE_WRITE_MAX_BYTES, "content")
    existed_before = path.exists()

    if existed_before and path.is_dir():
        return {"error": "path points to a directory"}

    if existed_before and not overwrite:
        return {"error": "file already exists and overwrite=false"}

    if create_dirs:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.parent.exists():
        return {"error": "parent directory does not exist"}

    path.write_bytes(encoded)
    return {
        "path": workspace_relpath(path),
        "bytes_written": len(encoded),
        "created": not existed_before,
    }


async def run_patch_file(args: Dict[str, Any]) -> Dict[str, Any]:
    path = resolve_workspace_path(str(args.get("path", "")).strip(), allow_missing=False)
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))
    replace_all = bool(args.get("replace_all", False))

    validate_utf8_size(old_text, PATCH_MAX_BYTES, "old_text")
    encoded_new_text = validate_utf8_size(new_text, PATCH_MAX_BYTES, "new_text")
    if not path.is_file():
        return {"error": "path must point to a file"}

    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"error": "file is not valid UTF-8"}

    if old_text:
        occurrences = content.count(old_text)
        if occurrences == 0:
            return {"error": "old_text was not found in file"}
        if occurrences > 1 and not replace_all:
            return {"error": "old_text occurs multiple times; set replace_all=true to replace all matches"}
        updated = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
        replacements = occurrences if replace_all else 1
    else:
        updated = content + new_text
        replacements = 1

    validate_utf8_size(updated, FILE_WRITE_MAX_BYTES, "patched file")
    path.write_bytes(updated.encode("utf-8"))
    return {
        "path": workspace_relpath(path),
        "replacements": replacements,
        "bytes_written": len(updated.encode("utf-8")),
        "appended": old_text == "",
        "new_text_bytes": len(encoded_new_text),
    }


async def run_mkdir(args: Dict[str, Any]) -> Dict[str, Any]:
    path = resolve_workspace_path(str(args.get("path", "")).strip(), allow_missing=True)
    parents = bool(args.get("parents", True))
    existed_before = path.exists()
    path.mkdir(parents=parents, exist_ok=True)
    return {
        "path": workspace_relpath(path),
        "created": not existed_before,
    }


# ---- Background process tools ----

async def run_start_bg_process(args: Dict[str, Any]) -> Dict[str, Any]:
    """Start a long-running process in the background without waiting for completion."""
    command = str(args.get("command", "")).strip()
    if not command:
        return {"error": "command is required"}

    try:
        parts = shlex.split(command)
    except Exception as exc:
        return {"error": f"invalid command: {exc}"}

    if not parts:
        return {"error": "command is empty"}

    cwd = resolve_workspace_path(str(args.get("cwd", ".")).strip(), allow_missing=False)
    if not cwd.is_dir():
        return {"error": "cwd must point to a directory"}

    executable = parts[0]
    executable_name = Path(executable).name if "/" in executable else executable
    if executable not in EXEC_COMMAND_ALLOWLIST and executable_name not in EXEC_COMMAND_ALLOWLIST:
        return {
            "error": "command is not in EXEC_COMMAND_ALLOWLIST",
            "allowed_commands": sorted(EXEC_COMMAND_ALLOWLIST),
        }

    resolved_executable = shutil.which(executable)
    if resolved_executable is None:
        local_candidate = resolve_workspace_path(str((cwd / executable).relative_to(WORKSPACE_ROOT)), allow_missing=False)
        if not local_candidate.is_file():
            return {"error": f"executable '{executable}' is not installed in the adapter runtime"}
        resolved_executable = str(local_candidate)

    parts[0] = resolved_executable

    env = os.environ.copy()
    extra_env = args.get("env", {})
    if extra_env:
        if not isinstance(extra_env, dict):
            return {"error": "env must be an object"}
        for key, value in extra_env.items():
            env[str(key)] = str(value)

    result = start_bg_process(parts, str(cwd), env)
    if "error" in result:
        return result
    return {
        "process_id": result["process_id"],
        "pid": result["pid"],
        "command": parts,
        "cwd": workspace_relpath(cwd),
    }


async def run_bg_process_status(args: Dict[str, Any]) -> Dict[str, Any]:
    """Get status and output of a background process by process_id."""
    process_id = str(args.get("process_id", "")).strip()
    if not process_id:
        return {"error": "process_id is required"}

    proc_info = get_bg_process(process_id)
    if proc_info is None:
        return {"error": "process not found"}

    return {
        "process_id": proc_info["process_id"],
        "pid": proc_info.get("pid"),
        "command": proc_info.get("command"),
        "cwd": proc_info.get("cwd"),
        "running": proc_info.get("running"),
        "returncode": proc_info.get("returncode"),
        "stdout": proc_info.get("stdout", ""),
        "stderr": proc_info.get("stderr", ""),
        "started_at": proc_info.get("started_at"),
        "finished_at": proc_info.get("finished_at"),
    }


async def run_list_bg_processes(args: Dict[str, Any]) -> Dict[str, Any]:
    """List all background processes."""
    processes = list_bg_processes()
    return {
        "processes": [
            {
                "process_id": p["process_id"],
                "pid": p.get("pid"),
                "command": p.get("command"),
                "running": p.get("running"),
                "returncode": p.get("returncode"),
                "started_at": p.get("started_at"),
            }
            for p in processes
        ],
        "count": len(processes),
    }


async def run_stop_bg_process(args: Dict[str, Any]) -> Dict[str, Any]:
    """Stop a running background process."""
    process_id = str(args.get("process_id", "")).strip()
    if not process_id:
        return {"error": "process_id is required"}

    result = stop_bg_process(process_id)
    if "error" in result:
        return result
    return {
        "process_id": process_id,
        "stopped": True,
        "returncode": result.get("returncode"),
    }


async def run_exec_command(args: Dict[str, Any]) -> Dict[str, Any]:
    command = str(args.get("command", "")).strip()
    if not command:
        return {"error": "command is required"}

    try:
        parts = shlex.split(command)
    except Exception as exc:
        return {"error": f"invalid command: {exc}"}

    if not parts:
        return {"error": "command is empty"}

    cwd = resolve_workspace_path(str(args.get("cwd", ".")).strip(), allow_missing=False)
    if not cwd.is_dir():
        return {"error": "cwd must point to a directory"}

    executable = parts[0]
    executable_name = Path(executable).name if "/" in executable else executable
    if executable not in EXEC_COMMAND_ALLOWLIST and executable_name not in EXEC_COMMAND_ALLOWLIST:
        return {
            "error": "command is not in EXEC_COMMAND_ALLOWLIST",
            "allowed_commands": sorted(EXEC_COMMAND_ALLOWLIST),
        }

    resolved_executable = shutil.which(executable)
    if resolved_executable is None:
        local_candidate = resolve_workspace_path(str((cwd / executable).relative_to(WORKSPACE_ROOT)), allow_missing=False)
        if not local_candidate.is_file():
            return {"error": f"executable '{executable}' is not installed in the adapter runtime"}
        resolved_executable = str(local_candidate)

    parts[0] = resolved_executable

    timeout_seconds = max(1, min(int(args.get("timeout_seconds", SHELL_TIMEOUT)), 600))
    env = os.environ.copy()
    extra_env = args.get("env", {})
    if extra_env:
        if not isinstance(extra_env, dict):
            return {"error": "env must be an object"}
        for key, value in extra_env.items():
            env[str(key)] = str(value)

    started_at = datetime.now().timestamp()
    try:
        result = subprocess.run(
            parts,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "command": parts,
            "cwd": workspace_relpath(cwd),
            "timed_out": True,
            "timeout_seconds": timeout_seconds,
            "stdout": (exc.stdout or "")[:COMMAND_OUTPUT_MAX_BYTES],
            "stderr": (exc.stderr or "")[:COMMAND_OUTPUT_MAX_BYTES],
            "duration_ms": int((datetime.now().timestamp() - started_at) * 1000),
        }

    return {
        "command": parts,
        "cwd": workspace_relpath(cwd),
        "returncode": result.returncode,
        "stdout": result.stdout[:COMMAND_OUTPUT_MAX_BYTES],
        "stderr": result.stderr[:COMMAND_OUTPUT_MAX_BYTES],
        "timed_out": False,
        "duration_ms": int((datetime.now().timestamp() - started_at) * 1000),
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
    return run_python_sync(args)


# ---- Sync wrappers for unit testing (same logic as async versions) ----

def run_python_sync(args: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous version of run_python for testing."""
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


def run_sqlite_query_sync(args: Dict[str, Any]) -> Dict[str, Any]:
    """Synchronous version of run_sqlite_query for testing."""
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


async def run_save_file(args: Dict[str, Any]) -> Dict[str, Any]:
    filename = str(args.get("filename", "")).strip()
    content = str(args.get("content", ""))

    if not filename:
        return {"error": "filename is required"}

    safe_name = os.path.basename(filename)
    if not safe_name:
        return {"error": "invalid filename"}

    validate_utf8_size(content, FILE_WRITE_MAX_BYTES, "content")
    os.makedirs(FILES_DIR, exist_ok=True)
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
    "browser_search": {
        "description": "Search the web with optional domain scoping for documentation or research workflows.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query to submit."},
                "domains": {"type": "array", "description": "Optional list of domains to prioritize with site: filters."},
                "provider": {"type": "string", "description": "Search provider: auto, google, or duckduckgo."},
                "max_results": {"type": "integer", "description": "Maximum number of search results to return, between 1 and 10."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "handler": run_browser_search,
    },
    "browser_open": {
        "description": "Open a web page and return structured metadata, headings, links and cleaned text.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http or https URL to open."},
                "max_links": {"type": "integer", "description": "Maximum number of links to include from the page."},
                "max_chars": {"type": "integer", "description": "Maximum number of text characters to return."},
                "timeout_seconds": {"type": "integer", "description": "Request timeout in seconds."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": run_browser_open,
    },
    "browser_extract": {
        "description": "Extract the main readable text from a web page for docs or article consumption.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http or https URL to extract from."},
                "strategy": {"type": "string", "description": "Extraction strategy: main or full."},
                "max_chars": {"type": "integer", "description": "Maximum number of text characters to return."},
                "timeout_seconds": {"type": "integer", "description": "Request timeout in seconds."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": run_browser_extract,
    },
    "browser_screenshot": {
        "description": "Take a PNG screenshot of a web page using a headless Chromium browser via Playwright. Returns base64-encoded PNG image.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute http or https URL to screenshot."},
                "viewport_width": {"type": "integer", "description": "Browser viewport width in pixels. Default 1280."},
                "viewport_height": {"type": "integer", "description": "Browser viewport height in pixels. Default 800."},
                "full_page": {"type": "boolean", "description": "Capture full scrollable page instead of just the viewport. Default false."},
                "timeout_seconds": {"type": "integer", "description": "Navigation timeout in seconds. Default 30."},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
        "handler": run_browser_screenshot,
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
        "description": "INTERNAL: Save adapter state or metadata files to /data/files. NOT for project code. For writing project files, use write_file instead.",
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
    "list_files": {
        "description": "List files and directories inside the workspace. Use this to inspect project structure.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path inside the workspace."},
                "recursive": {"type": "boolean", "description": "When true, walk nested directories recursively."},
                "max_entries": {"type": "integer", "description": "Maximum number of entries to return."},
            },
            "additionalProperties": False,
        },
        "handler": run_list_files,
    },
    "read_file": {
        "description": "Read a UTF-8 file from the workspace. Supports optional line ranges.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside the workspace."},
                "start_line": {"type": "integer", "description": "Optional starting line number, 1-based."},
                "end_line": {"type": "integer", "description": "Optional ending line number, 1-based."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": run_read_file,
    },
    "write_file": {
        "description": "Write a UTF-8 file inside the workspace. Use this to create or replace project files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside the workspace."},
                "content": {"type": "string", "description": "Full UTF-8 file content."},
                "create_dirs": {"type": "boolean", "description": "Create missing parent directories when true."},
                "overwrite": {"type": "boolean", "description": "Replace existing files when true."},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        "handler": run_write_file,
    },
    "patch_file": {
        "description": "Patch a UTF-8 workspace file by replacing exact text or appending when old_text is empty.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path inside the workspace."},
                "old_text": {"type": "string", "description": "Exact text to replace. Leave empty to append."},
                "new_text": {"type": "string", "description": "Replacement text or appended text."},
                "replace_all": {"type": "boolean", "description": "Replace every match of old_text when true."},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        "handler": run_patch_file,
    },
    "mkdir": {
        "description": "Create a directory inside the workspace.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path inside the workspace."},
                "parents": {"type": "boolean", "description": "Create missing parent directories when true."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "handler": run_mkdir,
    },
    "exec_command": {
        "description": "Run an allowlisted development command inside the workspace without invoking a shell implicitly.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to execute."},
                "cwd": {"type": "string", "description": "Relative working directory inside the workspace."},
                "timeout_seconds": {"type": "integer", "description": "Timeout in seconds."},
                "env": {"type": "object", "description": "Optional environment variable overrides."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "handler": run_exec_command,
    },
    "start_bg_process": {
        "description": "Start a long-running process in the background without waiting for it to finish. Use bg_process_status to check output and bg_process_stop to terminate it.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command line to execute."},
                "cwd": {"type": "string", "description": "Relative working directory inside the workspace."},
                "env": {"type": "object", "description": "Optional environment variable overrides."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "handler": run_start_bg_process,
    },
    "bg_process_status": {
        "description": "Get the current status, output and return code of a background process.",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "The process ID returned by start_bg_process."},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        },
        "handler": run_bg_process_status,
    },
    "list_bg_processes": {
        "description": "List all background processes started by this adapter session.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "handler": run_list_bg_processes,
    },
    "stop_bg_process": {
        "description": "Stop a running background process by its process_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "process_id": {"type": "string", "description": "The process ID returned by start_bg_process."},
            },
            "required": ["process_id"],
            "additionalProperties": False,
        },
        "handler": run_stop_bg_process,
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
        if expected_type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"field '{field}' must be a boolean")
        if expected_type == "object" and not isinstance(value, dict):
            raise ValueError(f"field '{field}' must be an object")


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


async def execute_tool_call(name: str, raw_args: Any, request_id: Optional[str] = None) -> Dict[str, Any]:
    METRICS["tool_requests_total"] += 1
    if name in EXEC_TOOLS:
        METRICS["tool_exec_calls"] += 1
    elif name in FS_TOOLS:
        METRICS["tool_fs_calls"] += 1
    start = datetime.now().timestamp()
    args = None
    result = None

    if name not in TOOL_REGISTRY:
        bump_metric_dict("tool_errors_total", name)
        bump_tool_status_metric(name, "not_found")
        error_result = {"error": f"tool '{name}' not found"}
        log_tool_call(name, {}, error_result, 0, request_id)
        if request_id and request_id in SESSION_STORE:
            append_tool_call(request_id, name, {}, error_result, 0)
        return error_result

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
        error_result = {"error": str(exc)}
        log_tool_call(name, args or {}, error_result, 0, request_id)
        if request_id and request_id in SESSION_STORE:
            append_tool_call(request_id, name, args or {}, error_result, 0)
        return error_result
    finally:
        duration_ms = (datetime.now().timestamp() - start) * 1000
        METRICS["tool_latency"].append(datetime.now().timestamp() - start)
        if result is not None:
            log_tool_call(name, args or {}, result, duration_ms, request_id)
            if request_id and request_id in SESSION_STORE:
                append_tool_call(request_id, name, args or {}, result, duration_ms)
