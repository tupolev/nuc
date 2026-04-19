# filepath: adapter/remote_tools.py
"""
Remote tool registry: allows the adapter to expose external tools
from remote sources (REST, MCP-gateway, etc.) as passthrough tools
in tool_execution_mode=client.

Architecture:
- RemoteToolSource: a source that provides one or more tools
  (e.g. a REST endpoint returning an OpenAPI spec, or an MCP gateway).
- RemoteToolRegistry: central registry managing registered sources.
- ToolMapping: maps remote tool schemas to OpenAI-compatible function specs.
- RetryConfig / TimeoutConfig: configurable retry and timeout per source.

The adapter in mode=client already supports passthrough of external tools
(section 8). This module extends that capability with:
1. Source registration and health checking.
2. Tool discovery from source (fetch tool list/schema).
3. Authentication (API key, Bearer token, Basic auth).
4. Mapping of remote schemas to OpenAI function format.
5. Automatic retry with exponential backoff.
6. Per-source timeout and error handling.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from config import (
    HTTP_TIMEOUT,
    TOOL_OUTPUT_MAX_LEN,
)

logger = logging.getLogger(__name__)


# ---- Dataclasses ----

@dataclass
class RetryConfig:
    """Retry configuration for a remote tool source."""
    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    retry_on_status: Tuple[int, ...] = (408, 429, 500, 502, 503, 504)
    retry_on_exception: Tuple[str, ...] = (
        "ConnectionError",
        "Timeout",
        "HTTPError",
    )


@dataclass
class TimeoutConfig:
    """Timeout configuration for a remote tool source."""
    connect_seconds: float = 5.0
    read_seconds: float = 30.0
    write_seconds: float = 10.0


@dataclass
class AuthConfig:
    """Authentication configuration for a remote tool source."""
    type: str = "none"  # "none", "api_key", "bearer", "basic"
    header_name: str = "X-API-Key"  # for api_key type
    api_key: str = ""
    bearer_token: str = ""
    username: str = ""
    password: str = ""


@dataclass
class RemoteToolSource:
    """
    A remote tool source. Provides tools from a remote endpoint.
    Can be a REST API returning OpenAI-compatible tool specs,
    or a gateway that exposes external tools.
    """
    name: str
    base_url: str
    tools_endpoint: str = "/tools"  # GET -> list of tool specs
    call_endpoint: str = "/call"     # POST -> call a tool
    auth: AuthConfig = field(default_factory=AuthConfig)
    timeout: TimeoutConfig = field(default_factory=TimeoutConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    headers: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    health_check_interval_seconds: float = 60.0

    def auth_headers(self) -> Dict[str, str]:
        h = dict(self.headers)
        if self.auth.type == "api_key":
            h[self.auth.header_name] = self.auth.api_key
        elif self.auth.type == "bearer":
            h["Authorization"] = f"Bearer {self.auth.bearer_token}"
        elif self.auth.type == "basic":
            import base64
            credentials = f"{self.auth.username}:{self.auth.password}"
            h["Authorization"] = f"Basic {base64.b64encode(credentials.encode()).decode()}"
        return h


@dataclass
class DiscoveredTool:
    """A tool discovered from a remote source."""
    source_name: str
    name: str  # unique within source, may be prefixed
    display_name: str  # user-facing name
    description: str
    parameters: Dict[str, Any]  # OpenAPI schema
    original_schema: Dict[str, Any]  # raw schema from source
    auth_required: bool = False
    retry_config: Optional[RetryConfig] = None


# ---- RemoteToolRegistry ----

class RemoteToolRegistry:
    """
    Registry of remote tool sources. Manages discovery, health
    checks, and provides a unified interface to all remote tools.
    """

    def __init__(self) -> None:
        self._sources: Dict[str, RemoteToolSource] = {}
        self._tools: Dict[str, DiscoveredTool] = {}  # key: "source_name/tool_name"
        self._health_tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    def register_source(self, source: RemoteToolSource) -> None:
        """Register a remote tool source. Replaces existing source with same name."""
        self._sources[source.name] = source
        logger.info("remote_tool_registry: registered source '%s' at %s", source.name, source.base_url)

    def unregister_source(self, name: str) -> None:
        """Remove a source and all its tools."""
        if name in self._sources:
            del self._sources[name]
        to_remove = [k for k in self._tools if k.startswith(f"{name}/")]
        for key in to_remove:
            del self._tools[key]
        if name in self._health_tasks:
            self._health_tasks[name].cancel()
            del self._health_tasks[name]
        logger.info("remote_tool_registry: unregistered source '%s'", name)

    async def discover_tools(self, source_name: str) -> List[DiscoveredTool]:
        """
        Discover tools from a registered source by calling its /tools endpoint.
        Returns list of DiscoveredTool objects. Updates internal registry.
        """
        source = self._sources.get(source_name)
        if source is None:
            logger.warning("remote_tool_registry: unknown source '%s'", source_name)
            return []

        if not source.enabled:
            logger.info("remote_tool_registry: source '%s' is disabled, skipping discovery", source_name)
            return []

        tools = await self._fetch_tool_list(source)
        discovered = []
        for tool in tools:
            key = f"{source_name}/{tool['name']}"
            dt = DiscoveredTool(
                source_name=source_name,
                name=tool["name"],
                display_name=tool.get("display_name", tool["name"]),
                description=tool.get("description", ""),
                parameters=tool.get("parameters", {}),
                original_schema=tool,
                auth_required=tool.get("auth_required", False),
                retry_config=source.retry,
            )
            self._tools[key] = dt
            discovered.append(dt)

        logger.info("remote_tool_registry: discovered %d tools from source '%s'", len(discovered), source_name)
        return discovered

    async def discover_all(self) -> List[DiscoveredTool]:
        """Discover tools from all registered and enabled sources."""
        all_tools: List[DiscoveredTool] = []
        for name in list(self._sources.keys()):
            tools = await self.discover_tools(name)
            all_tools.extend(tools)
        return all_tools

    def get_tool(self, source_name: str, tool_name: str) -> Optional[DiscoveredTool]:
        """Get a specific discovered tool."""
        return self._tools.get(f"{source_name}/{tool_name}")

    def get_tools_by_source(self, source_name: str) -> List[DiscoveredTool]:
        """Get all discovered tools from a source."""
        return [t for k, t in self._tools.items() if t.source_name == source_name]

    def all_tools(self) -> List[DiscoveredTool]:
        """Get all discovered tools from all sources."""
        return list(self._tools.values())

    def build_openai_tools(self) -> List[Dict[str, Any]]:
        """
        Build OpenAI-compatible tool specs from all discovered tools.
        This is the mapping from remote schema -> OpenAI function spec.
        """
        specs = []
        for tool in self._tools.values():
            spec = {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            specs.append(spec)
        return specs

    async def call_remote_tool(
        self,
        source_name: str,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Call a remote tool with arguments. Uses retry and timeout from source config.
        Returns the result or an error dict.
        """
        tool = self.get_tool(source_name, tool_name)
        if tool is None:
            return {"error": f"tool '{tool_name}' not found in source '{source_name}'"}

        source = self._sources[source_name]
        return await self._call_with_retry(source, tool_name, arguments)

    async def _fetch_tool_list(self, source: RemoteToolSource) -> List[Dict[str, Any]]:
        """Fetch tool list from source /tools endpoint."""
        url = f"{source.base_url.rstrip('/')}{source.tools_endpoint}"
        attempt = 0
        delay = source.retry.base_delay_seconds

        while attempt < source.retry.max_attempts:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=source.timeout.connect_seconds,
                    read=source.timeout.read_seconds,
                    write=source.timeout.write_seconds,
                )) as client:
                    response = await client.get(url, headers=source.auth_headers())
                    if response.status_code in source.retry.retry_on_status:
                        logger.warning("remote_tool_registry: got %d from %s, retry %d/%d",
                                       response.status_code, source.name, attempt, source.retry.max_attempts)
                    else:
                        response.raise_for_status()
                        data = response.json()
                        if isinstance(data, dict) and "tools" in data:
                            return data["tools"]
                        if isinstance(data, list):
                            return data
                        logger.warning("remote_tool_registry: unexpected response from %s tools endpoint", source.name)
                        return []
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in source.retry.retry_on_status:
                    logger.warning("remote_tool_registry: HTTP %d from %s, retry %d/%d",
                                   exc.response.status_code, source.name, attempt, source.retry.max_attempts)
                else:
                    logger.error("remote_tool_registry: HTTP error from %s: %s", source.name, exc)
                    return []
            except Exception as exc:
                exc_name = type(exc).__name__
                if any(pattern in exc_name for pattern in source.retry.retry_on_exception):
                    logger.warning("remote_tool_registry: %s from %s, retry %d/%d: %s",
                                   exc_name, source.name, attempt, source.retry.max_attempts, exc)
                else:
                    logger.error("remote_tool_registry: unexpected error from %s: %s", source.name, exc)
                    return []

            if attempt < source.retry.max_attempts:
                await asyncio.sleep(min(delay, source.retry.max_delay_seconds))
                delay *= 2

        logger.error("remote_tool_registry: all retries exhausted for %s tools endpoint", source.name)
        return []

    async def _call_with_retry(
        self,
        source: RemoteToolSource,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Call a remote tool with exponential backoff retry."""
        url = f"{source.base_url.rstrip('/')}{source.call_endpoint}"
        body = {"name": tool_name, "arguments": arguments}
        attempt = 0
        delay = source.retry.base_delay_seconds
        last_error = ""

        while attempt < source.retry.max_attempts:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(
                    connect=source.timeout.connect_seconds,
                    read=source.timeout.read_seconds,
                    write=source.timeout.write_seconds,
                )) as client:
                    response = await client.post(
                        url,
                        json=body,
                        headers={**source.auth_headers(), "Content-Type": "application/json"},
                    )
                    if response.status_code in source.retry.retry_on_status:
                        last_error = f"HTTP {response.status_code}"
                        logger.warning("remote_tool_registry: %s from %s for tool '%s', retry %d/%d",
                                       last_error, source.name, tool_name, attempt, source.retry.max_attempts)
                    else:
                        response.raise_for_status()
                        result = response.json()
                        if isinstance(result, dict) and "error" in result:
                            return result
                        return result
            except httpx.HTTPStatusError as exc:
                last_error = f"HTTP {exc.response.status_code}"
                if exc.response.status_code in source.retry.retry_on_status:
                    logger.warning("remote_tool_registry: %s from %s for tool '%s', retry %d/%d",
                                   last_error, source.name, tool_name, attempt, source.retry.max_attempts)
                else:
                    logger.error("remote_tool_registry: HTTP error from %s for tool '%s': %s",
                                source.name, tool_name, exc)
                    return {"error": f"remote tool error: {exc}"}
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                exc_name = type(exc).__name__
                if any(pattern in exc_name for pattern in source.retry.retry_on_exception):
                    logger.warning("remote_tool_registry: %s from %s for tool '%s', retry %d/%d",
                                   exc_name, source.name, tool_name, attempt, source.retry.max_attempts)
                else:
                    logger.error("remote_tool_registry: unexpected error calling tool '%s' from %s: %s",
                                tool_name, source.name, exc)
                    return {"error": f"remote tool error: {exc}"}

            if attempt < source.retry.max_attempts:
                await asyncio.sleep(min(delay, source.retry.max_delay_seconds))
                delay *= 2

        return {"error": f"remote tool call failed after {attempt} attempts: {last_error}"}

    async def health_check(self, source_name: str) -> bool:
        """
        Perform a health check on a source by calling its /tools endpoint
        with a short timeout. Returns True if healthy, False otherwise.
        """
        source = self._sources.get(source_name)
        if source is None:
            return False

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(connect=3.0, read=5.0)) as client:
                response = await client.get(
                    f"{source.base_url.rstrip('/')}{source.tools_endpoint}",
                    headers=source.auth_headers(),
                )
                if response.status_code < 500:
                    return True
        except Exception as exc:
            logger.debug("health_check failed for source '%s': %s", source_name, exc)
        return False

    async def _health_check_loop(self, source_name: str) -> None:
        """Background health check loop for a source."""
        source = self._sources.get(source_name)
        if source is None:
            return
        while True:
            await asyncio.sleep(source.health_check_interval_seconds)
            healthy = await self.health_check(source_name)
            source.enabled = healthy
            if not healthy:
                logger.warning("remote_tool_registry: source '%s' is unhealthy, disabling", source_name)
            else:
                logger.debug("remote_tool_registry: source '%s' is healthy", source_name)

    def start_health_checks(self) -> None:
        """Start background health check tasks for all registered sources."""
        for name in self._sources:
            if name not in self._health_tasks:
                self._health_tasks[name] = asyncio.create_task(self._health_check_loop(name))

    def stop_health_checks(self) -> None:
        """Stop all background health check tasks."""
        for task in self._health_tasks.values():
            task.cancel()
        self._health_tasks.clear()


# ---- Module-level registry singleton ----

_registry: Optional[RemoteToolRegistry] = None


def get_registry() -> RemoteToolRegistry:
    global _registry
    if _registry is None:
        _registry = RemoteToolRegistry()
    return _registry


def register_remote_source(
    name: str,
    base_url: str,
    auth_type: str = "none",
    api_key: str = "",
    bearer_token: str = "",
    username: str = "",
    password: str = "",
    tools_endpoint: str = "/tools",
    call_endpoint: str = "/call",
    max_retries: int = 3,
    timeout_connect: float = 5.0,
    timeout_read: float = 30.0,
) -> None:
    """
    Convenience function to register a remote tool source from config.
    Call this from adapter startup or config loading.
    """
    registry = get_registry()
    auth = AuthConfig(
        type=auth_type,
        api_key=api_key,
        bearer_token=bearer_token,
        username=username,
        password=password,
    )
    source = RemoteToolSource(
        name=name,
        base_url=base_url,
        auth=auth,
        tools_endpoint=tools_endpoint,
        call_endpoint=call_endpoint,
        retry=RetryConfig(max_attempts=max_retries),
        timeout=TimeoutConfig(connect_seconds=timeout_connect, read_seconds=timeout_read),
    )
    registry.register_source(source)


# ---- Tool schema mapping utilities ----

def map_openapi_to_openai(params: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map an OpenAPI 3.x parameter schema to OpenAI function parameter schema.
    Handles type conversions, required fields, descriptions.
    """
    properties = {}
    required: List[str] = []

    for prop_name, prop_def in params.get("properties", {}).items():
        mapped_type = _map_type(prop_def.get("type", "string"))
        prop_schema = {
            "type": mapped_type,
            "description": prop_def.get("description", ""),
        }
        if mapped_type == "string" and prop_def.get("enum"):
            prop_schema["enum"] = prop_def["enum"]
        properties[prop_name] = prop_schema

    if params.get("required"):
        required = params["required"]

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": params.get("additionalProperties", False),
    }


def _map_type(openapi_type: str) -> str:
    """Map OpenAPI type to OpenAI parameter type."""
    mapping = {
        "string": "string",
        "number": "number",
        "integer": "integer",
        "boolean": "boolean",
        "array": "array",
        "object": "object",
    }
    return mapping.get(openapi_type, "string")


def map_remote_schema_to_openai(tool_schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Map a remote tool schema (arbitrary format) to OpenAI-compatible function spec.
    Handles common formats: OpenAPI 3.x, MCP schema, custom JSON schemas.
    """
    func_def = tool_schema.get("function", tool_schema.get("function_def", {}))
    raw_params = func_def.get("parameters", {})

    # If parameters is already an OpenAI-style schema, use it directly
    if raw_params.get("type") == "object" and "properties" in raw_params:
        parameters = raw_params
    elif "properties" in raw_params:
        # OpenAPI-style
        parameters = map_openapi_to_openai(raw_params)
    else:
        # Fallback: wrap raw params as properties
        parameters = {
            "type": "object",
            "properties": {"input": {"type": "string", "description": "Tool input"}},
            "required": [],
            "additionalProperties": True,
        }

    return {
        "type": "function",
        "function": {
            "name": func_def.get("name", tool_schema.get("name", "unknown")),
            "description": func_def.get("description", tool_schema.get("description", "")),
            "parameters": parameters,
        },
    }