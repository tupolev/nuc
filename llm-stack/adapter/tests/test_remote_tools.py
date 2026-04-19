# filepath: adapter/tests/test_remote_tools.py
"""
Tests for remote_tools.py: RemoteToolRegistry, tool discovery,
auth, mapping, retries, and health checks.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from remote_tools import (
    AuthConfig,
    DiscoveredTool,
    RemoteToolSource,
    RemoteToolRegistry,
    RetryConfig,
    TimeoutConfig,
    map_openapi_to_openai,
    map_remote_schema_to_openai,
    _map_type,
    get_registry,
    register_remote_source,
)


# --- _map_type ---

def test_map_type_string():
    assert _map_type("string") == "string"


def test_map_type_integer():
    assert _map_type("integer") == "integer"


def test_map_type_boolean():
    assert _map_type("boolean") == "boolean"


def test_map_type_unknown_defaults_to_string():
    assert _map_type("file") == "string"
    assert _map_type("uuid") == "string"


# --- map_openapi_to_openai ---

def test_map_openapi_to_openai_basic():
    params = {
        "properties": {
            "query": {"type": "string", "description": "Search query"},
            "limit": {"type": "integer", "description": "Max results"},
        },
        "required": ["query"],
    }
    result = map_openapi_to_openai(params)
    assert result["type"] == "object"
    assert "query" in result["properties"]
    assert "limit" in result["properties"]
    assert result["properties"]["query"]["type"] == "string"
    assert result["properties"]["limit"]["type"] == "integer"
    assert result["required"] == ["query"]


def test_map_openapi_to_openai_with_enum():
    params = {
        "properties": {
            "status": {"type": "string", "enum": ["active", "inactive"]},
        },
    }
    result = map_openapi_to_openai(params)
    assert result["properties"]["status"]["enum"] == ["active", "inactive"]


def test_map_openapi_to_openai_additional_properties():
    params = {
        "properties": {"name": {"type": "string"}},
        "additionalProperties": True,
    }
    result = map_openapi_to_openai(params)
    assert result["additionalProperties"] is True


# --- map_remote_schema_to_openai ---

def test_map_remote_schema_openai_style():
    schema = {
        "name": "my_tool",
        "description": "Does something",
        "function": {
            "name": "my_tool",
            "description": "Does something",
            "parameters": {
                "type": "object",
                "properties": {"msg": {"type": "string"}},
                "required": ["msg"],
            },
        },
    }
    result = map_remote_schema_to_openai(schema)
    assert result["type"] == "function"
    assert result["function"]["name"] == "my_tool"
    assert result["function"]["description"] == "Does something"
    assert result["function"]["parameters"]["properties"]["msg"]["type"] == "string"


def test_map_remote_schema_openapi_style_no_function_wrapper():
    """Some schemas have function_def instead of function."""
    schema = {
        "name": "web_search",
        "description": "Search the web",
        "function_def": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
        },
    }
    result = map_remote_schema_to_openai(schema)
    assert result["function"]["name"] == "web_search"


def test_map_remote_schema_fallback():
    """Unknown format falls back to generic input wrapper."""
    schema = {"name": "unknown_tool", "description": "desc", "custom": "data"}
    result = map_remote_schema_to_openai(schema)
    assert result["function"]["name"] == "unknown_tool"
    props = result["function"]["parameters"]["properties"]
    assert "input" in props


# --- RemoteToolSource ---

def test_remote_tool_source_defaults():
    source = RemoteToolSource(name="test", base_url="http://example.com")
    assert source.name == "test"
    assert source.enabled is True
    assert source.auth.type == "none"
    assert source.retry.max_attempts == 3
    assert source.timeout.connect_seconds == 5.0


def test_remote_tool_source_auth_headers_api_key():
    source = RemoteToolSource(
        name="test",
        base_url="http://example.com",
        auth=AuthConfig(type="api_key", header_name="X-Key", api_key="secret"),
    )
    h = source.auth_headers()
    assert h["X-Key"] == "secret"


def test_remote_tool_source_auth_headers_bearer():
    source = RemoteToolSource(
        name="test",
        base_url="http://example.com",
        auth=AuthConfig(type="bearer", bearer_token="tok123"),
    )
    h = source.auth_headers()
    assert h["Authorization"] == "Bearer tok123"


def test_remote_tool_source_auth_headers_basic():
    source = RemoteToolSource(
        name="test",
        base_url="http://example.com",
        auth=AuthConfig(type="basic", username="user", password="pass"),
    )
    h = source.auth_headers()
    assert h["Authorization"].startswith("Basic ")


# --- RemoteToolRegistry ---

def test_registry_register_source():
    registry = RemoteToolRegistry()
    source = RemoteToolSource(name="src1", base_url="http://src1.com")
    registry.register_source(source)
    assert "src1" in registry._sources


def test_registry_unregister_source():
    registry = RemoteToolRegistry()
    source = RemoteToolSource(name="src1", base_url="http://src1.com")
    registry.register_source(source)
    registry.unregister_source("src1")
    assert "src1" not in registry._sources


def test_registry_discover_tools_unknown_source():
    registry = RemoteToolRegistry()
    tools = asyncio.run(registry.discover_tools("unknown"))
    assert tools == []


def test_registry_discover_tools_disabled_source():
    registry = RemoteToolRegistry()
    source = RemoteToolSource(name="disabled", base_url="http://example.com", enabled=False)
    registry.register_source(source)
    tools = asyncio.run(registry.discover_tools("disabled"))
    assert tools == []


@pytest.mark.asyncio
async def test_registry_discover_tools_from_list():
    """Registry fetches and stores tool list from source /tools endpoint."""
    registry = RemoteToolRegistry()
    source = RemoteToolSource(name="remote1", base_url="http://remote1.com")
    registry.register_source(source)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "tools": [
            {
                "name": "weather",
                "description": "Get weather",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string", "description": "City name"}},
                    "required": ["city"],
                },
            },
            {
                "name": "stock",
                "description": "Get stock price",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                },
            },
        ]
    }

    with patch("httpx.AsyncClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_instance.get = AsyncMock(return_value=mock_response)
        MockClient.return_value = mock_instance

        tools = await registry.discover_tools("remote1")

    assert len(tools) == 2
    assert tools[0].name == "weather"
    assert tools[0].source_name == "remote1"
    assert tools[1].name == "stock"

    # Verify stored in registry
    assert registry.get_tool("remote1", "weather") is not None
    assert registry.get_tool("remote1", "stock") is not None


def test_registry_build_openai_tools():
    registry = RemoteToolRegistry()
    # Manually inject a discovered tool
    tool = DiscoveredTool(
        source_name="src",
        name="search",
        display_name="Search",
        description="Search the web",
        parameters={
            "type": "object",
            "properties": {"q": {"type": "string", "description": "Query"}},
            "required": ["q"],
        },
        original_schema={},
    )
    registry._tools["src/search"] = tool

    specs = registry.build_openai_tools()
    assert len(specs) == 1
    assert specs[0]["type"] == "function"
    assert specs[0]["function"]["name"] == "search"


def test_registry_get_tools_by_source():
    registry = RemoteToolRegistry()
    tool1 = DiscoveredTool(source_name="src", name="t1", display_name="T1", description="", parameters={}, original_schema={})
    tool2 = DiscoveredTool(source_name="src", name="t2", display_name="T2", description="", parameters={}, original_schema={})
    tool3 = DiscoveredTool(source_name="other", name="t3", display_name="T3", description="", parameters={}, original_schema={})
    registry._tools["src/t1"] = tool1
    registry._tools["src/t2"] = tool2
    registry._tools["other/t3"] = tool3

    src_tools = registry.get_tools_by_source("src")
    assert len(src_tools) == 2
    other_tools = registry.get_tools_by_source("other")
    assert len(other_tools) == 1


def test_registry_all_tools():
    registry = RemoteToolRegistry()
    tool = DiscoveredTool(source_name="s", name="t", display_name="T", description="", parameters={}, original_schema={})
    registry._tools["s/t"] = tool
    assert len(registry.all_tools()) == 1


def test_registry_get_tool_not_found():
    registry = RemoteToolRegistry()
    assert registry.get_tool("nonexistent", "tool") is None


@pytest.mark.asyncio
async def test_registry_health_check_healthy():
    registry = RemoteToolRegistry()
    source = RemoteToolSource(name="healthy", base_url="http://healthy.com")
    registry.register_source(source)

    mock_response = MagicMock()
    mock_response.status_code = 200

    with patch("httpx.AsyncClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_instance.get = AsyncMock(return_value=mock_response)
        MockClient.return_value = mock_instance

        healthy = await registry.health_check("healthy")

    assert healthy is True


@pytest.mark.asyncio
async def test_registry_health_check_unhealthy():
    registry = RemoteToolRegistry()
    source = RemoteToolSource(name="sick", base_url="http://sick.com")
    registry.register_source(source)

    with patch("httpx.AsyncClient") as MockClient:
        mock_instance = AsyncMock()
        mock_instance.__aenter__.return_value = mock_instance
        mock_instance.__aexit__.return_value = None
        mock_instance.get = AsyncMock(side_effect=Exception("connection failed"))
        MockClient.return_value = mock_instance

        healthy = await registry.health_check("sick")

    assert healthy is False


# --- register_remote_source convenience function ---

def test_register_remote_source():
    register_remote_source(
        name="rest_api",
        base_url="http://api.example.com",
        auth_type="api_key",
        api_key="key-123",
        max_retries=5,
        timeout_connect=3.0,
        timeout_read=60.0,
    )
    registry = get_registry()
    assert "rest_api" in registry._sources
    src = registry._sources["rest_api"]
    assert src.auth.type == "api_key"
    assert src.auth.api_key == "key-123"
    assert src.retry.max_attempts == 5
    assert src.timeout.connect_seconds == 3.0
    assert src.timeout.read_seconds == 60.0


# --- DiscoveredTool ---

def test_discovered_tool_defaults():
    tool = DiscoveredTool(
        source_name="src",
        name="test",
        display_name="Test",
        description="desc",
        parameters={},
        original_schema={},
    )
    assert tool.auth_required is False
    assert tool.retry_config is None
