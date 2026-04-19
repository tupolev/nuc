# filepath: adapter/tests/test_remote_tools_integration.py
"""
Integration tests for RemoteToolRegistry.
Uses a local FastAPI app as the remote tool source, avoiding real network calls.
"""

from typing import Any, Dict

import pytest
from fastapi import FastAPI
from fastapi.testclient import AsyncClient

from remote_tools import (
    AuthConfig,
    DiscoveredTool,
    RemoteToolRegistry,
    RetryConfig,
)


# ---- Minimal test server that implements the remote tool protocol ----

test_app = FastAPI()

TOOL_DEFINITIONS = [
    {
        "name": "calculator",
        "description": "Add two integers",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "First number"},
                "b": {"type": "integer", "description": "Second number"},
            },
            "required": ["a", "b"],
        },
    },
    {
        "name": "uppercase",
        "description": "Convert text to uppercase",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to uppercase"},
            },
            "required": ["text"],
        },
    },
]


@test_app.get("/tools")
async def list_tools():
    return {"tools": TOOL_DEFINITIONS}


@test_app.post("/call")
async def call_tool(body: Dict[str, Any]):
    name = body.get("name")
    arguments = body.get("arguments", {})
    if name == "calculator":
        return {"result": arguments.get("a", 0) + arguments.get("b", 0)}
    if name == "uppercase":
        return {"upper": arguments.get("text", "").upper()}
    return {"error": f"unknown tool: {name}"}


# ---- Helpers ----

async def use_test_client(registry: RemoteToolRegistry):
    """
    Patch RemoteToolRegistry httpx.AsyncClient to use our test app.
    Returns original methods so tests can restore them.
    """
    import remote_tools as rt

    orig_get = rt.httpx.AsyncClient.get
    orig_request = rt.httpx.AsyncClient.request

    async def patched_get(self, url, **kwargs):
        async with AsyncClient(app=test_app, base_url="http://test") as c:
            return await c.get(url, **kwargs)

    async def patched_request(self, method, url, **kwargs):
        async with AsyncClient(app=test_app, base_url="http://test") as c:
            return await c.request(method, url, **kwargs)

    rt.httpx.AsyncClient.get = patched_get
    rt.httpx.AsyncClient.request = patched_request

    return orig_get, orig_request


# ---- Tests ----

@pytest.mark.asyncio
async def test_remote_tool_discovery_stores_tools():
    """discover_tools() fetches from /tools and stores DiscoveredTool objects."""
    import remote_tools as rt

    registry = RemoteToolRegistry()
    source = rt.RemoteToolSource(name="test_src", base_url="http://test")
    registry.register_source(source)

    orig_get, _ = await use_test_client(registry)
    try:
        tools = await registry.discover_tools("test_src")
    finally:
        rt.httpx.AsyncClient.get = orig_get

    assert len(tools) == 2
    tool_names = {t.name for t in tools}
    assert "calculator" in tool_names
    assert "uppercase" in tool_names


@pytest.mark.asyncio
async def test_remote_tool_call_returns_result():
    """call_remote_tool() returns the result from the remote /call endpoint."""
    import remote_tools as rt

    registry = RemoteToolRegistry()
    source = rt.RemoteToolSource(
        name="test_src",
        base_url="http://test",
        retry=RetryConfig(max_attempts=1),
    )
    registry.register_source(source)

    orig_get, orig_request = await use_test_client(registry)
    try:
        await registry.discover_tools("test_src")
        result = await registry.call_remote_tool("test_src", "calculator", {"a": 5, "b": 7})
    finally:
        rt.httpx.AsyncClient.get = orig_get
        rt.httpx.AsyncClient.request = orig_request

    assert "error" not in result
    assert result["result"] == 12


@pytest.mark.asyncio
async def test_remote_tool_call_unknown_tool_returns_error():
    """Calling an unknown tool returns an error dict."""
    import remote_tools as rt

    registry = RemoteToolRegistry()
    source = rt.RemoteToolSource(
        name="test_src",
        base_url="http://test",
        retry=RetryConfig(max_attempts=1),
    )
    registry.register_source(source)

    orig_get, orig_request = await use_test_client(registry)
    try:
        await registry.discover_tools("test_src")
        result = await registry.call_remote_tool("test_src", "nonexistent", {})
    finally:
        rt.httpx.AsyncClient.get = orig_get
        rt.httpx.AsyncClient.request = orig_request

    assert "error" in result


@pytest.mark.asyncio
async def test_build_openai_tools_returns_function_specs():
    """build_openai_tools() returns OpenAI-compatible tool specs."""
    registry = RemoteToolRegistry()

    tool = DiscoveredTool(
        source_name="src",
        name="my_tool",
        display_name="My Tool",
        description="Does something",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
        original_schema={},
    )
    registry._tools["src/my_tool"] = tool

    specs = registry.build_openai_tools()
    assert len(specs) == 1
    assert specs[0]["type"] == "function"
    assert specs[0]["function"]["name"] == "my_tool"
    assert specs[0]["function"]["description"] == "Does something"
    assert "query" in specs[0]["function"]["parameters"]["properties"]


@pytest.mark.asyncio
async def test_auth_api_key_header():
    """Auth type api_key sets the correct header via auth_headers()."""
    import remote_tools as rt

    source = rt.RemoteToolSource(
        name="auth_test",
        base_url="http://auth-test.com",
        auth=AuthConfig(type="api_key", header_name="X-API-Key", api_key="secret-key"),
    )
    headers = source.auth_headers()
    assert headers["X-API-Key"] == "secret-key"


@pytest.mark.asyncio
async def test_discover_all_from_multiple_sources():
    """discover_all() aggregates tools from all registered sources."""
    registry = RemoteToolRegistry()

    tool1 = DiscoveredTool(
        source_name="src1", name="tool_a", display_name="Tool A",
        description="", parameters={}, original_schema={},
    )
    tool2 = DiscoveredTool(
        source_name="src2", name="tool_b", display_name="Tool B",
        description="", parameters={}, original_schema={},
    )
    registry._tools["src1/tool_a"] = tool1
    registry._tools["src2/tool_b"] = tool2

    all_tools = registry.discover_all()
    assert len(all_tools) == 2
    names = {t.name for t in all_tools}
    assert "tool_a" in names
    assert "tool_b" in names


@pytest.mark.asyncio
async def test_unregister_source_removes_tools():
    """unregister_source() removes the source and all its tools."""
    import remote_tools as rt

    registry = RemoteToolRegistry()
    tool = DiscoveredTool(
        source_name="to_remove", name="t1", display_name="T1",
        description="", parameters={}, original_schema={},
    )
    registry._tools["to_remove/t1"] = tool
    source = rt.RemoteToolSource(name="to_remove", base_url="http://x.com")
    registry.register_source(source)

    registry.unregister_source("to_remove")

    assert "to_remove" not in registry._sources
    assert "to_remove/t1" not in registry._tools
