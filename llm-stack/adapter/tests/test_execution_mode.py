# filepath: tests/test_execution_mode.py
"""Tests for tool_execution_mode: server vs client behavior."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openai_compat import (
    normalize_execution_mode,
    build_effective_tools,
    normalize_local_openai_tools,
    normalize_passthrough_openai_tools,
)
from config import TOOL_EXECUTION_MODE


class TestNormalizeExecutionMode:
    """Tests for normalize_execution_mode()."""

    def test_server_returns_server(self):
        assert normalize_execution_mode("server") == "server"

    def test_client_returns_client(self):
        assert normalize_execution_mode("client") == "client"

    def test_case_insensitive(self):
        assert normalize_execution_mode("SERVER") == "server"
        assert normalize_execution_mode("Client") == "client"
        assert normalize_execution_mode("SeRvEr") == "server"

    def test_invalid_returns_configured_safe_default(self):
        assert normalize_execution_mode("invalid") == TOOL_EXECUTION_MODE
        assert normalize_execution_mode("") == TOOL_EXECUTION_MODE
        assert normalize_execution_mode(None) == TOOL_EXECUTION_MODE

    def test_common_aliases_are_supported(self):
        assert normalize_execution_mode("local") == "client"
        assert normalize_execution_mode("host") == "client"
        assert normalize_execution_mode("remote") == "server"

    def test_default_from_config(self):
        """When called with None, uses TOOL_EXECUTION_MODE from config."""
        result = normalize_execution_mode(None)
        assert result in ("server", "client")


class TestBuildEffectiveToolsServerMode:
    """Tests for build_effective_tools() in server mode."""

    def test_server_mode_returns_local_tools(self):
        """Server mode returns local tools when explicitly requested."""
        local_tools = [{"type": "function", "function": {"name": "list_files", "description": "", "parameters": {}}}]
        result = build_effective_tools(local_tools, None, "server")
        assert len(result) >= 1
        assert any(t["function"]["name"] == "list_files" for t in result)

    def test_server_mode_unknown_tool_rejected(self):
        """Server mode rejects tools not in TOOL_REGISTRY."""
        unknown_tools = [{"type": "function", "function": {"name": "nonexistent_tool", "description": "", "parameters": {}}}]
        result = build_effective_tools(unknown_tools, None, "server")
        assert result == []

    def test_server_mode_empty_returns_all_local_when_auto_enabled(self):
        """Server mode with no tools and AUTO_ENABLE_LOCAL_TOOLS returns all specs."""
        from config import AUTO_ENABLE_LOCAL_TOOLS
        if AUTO_ENABLE_LOCAL_TOOLS:
            result = build_effective_tools(None, None, "server")
            assert len(result) > 0


class TestBuildEffectiveToolsClientMode:
    """Tests for build_effective_tools() in client mode."""

    def test_client_mode_returns_passthrough_tools(self):
        """Client mode returns passthrough (client) tools."""
        client_tools = [{"type": "function", "function": {"name": "my_custom_tool", "description": "A custom tool", "parameters": {"type": "object", "properties": {}}}}]
        result = build_effective_tools(client_tools, None, "client")
        assert len(result) >= 1
        assert any(t["function"]["name"] == "my_custom_tool" for t in result)

    def test_client_mode_keeps_local_name_collisions_for_passthrough(self):
        """Client mode keeps colliding names so the client can execute its own tools."""
        client_tools = [
            {"type": "function", "function": {"name": "list_files", "description": "Local collision", "parameters": {}}},
            {"type": "function", "function": {"name": "my_custom_tool", "description": "Custom tool", "parameters": {}}},
        ]
        result = build_effective_tools(client_tools, None, "client")
        names = {t["function"]["name"] for t in result}
        assert "list_files" in names
        assert "my_custom_tool" in names

    def test_client_mode_preserves_local_named_tool_as_passthrough(self):
        """Client mode preserves a local-looking tool name for client-side execution."""
        local_tools = [{"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}]
        result = build_effective_tools(local_tools, None, "client")
        assert len(result) == 1
        assert result[0]["function"]["name"] == "read_file"

    def test_client_mode_empty_no_auto_enable(self):
        """Client mode with no tools and no auto-enable returns empty."""
        from config import AUTO_ENABLE_LOCAL_TOOLS
        result = build_effective_tools(None, None, "client")
        if not AUTO_ENABLE_LOCAL_TOOLS:
            assert result == []


class TestExecutionModeEndToEnd:
    """Integration-style tests for execution_mode flow."""

    def test_execution_mode_param_accepted(self):
        """The normalize_execution_mode function accepts both string forms."""
        assert normalize_execution_mode("server") == "server"
        assert normalize_execution_mode("client") == "client"

    def test_server_mode_tool_not_rejected_for_unknown(self):
        """In server mode, a valid local tool name is not rejected even if not in requested list logic."""
        # This tests the normalize_local path: unknown tools get filtered
        unknown = [{"type": "function", "function": {"name": "web_search", "description": "", "parameters": {}}}]
        result = build_effective_tools(unknown, None, "server")
        # web_search IS in TOOL_REGISTRY, so it should pass
        names = {t["function"]["name"] for t in result}
        assert "web_search" in names

    def test_client_mode_passthrough_with_multiple_custom_tools(self):
        """Client mode correctly passes through multiple custom tools."""
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "tool_b", "description": "B", "parameters": {}}},
            {"type": "function", "function": {"name": "tool_c", "description": "C", "parameters": {}}},
        ]
        result = build_effective_tools(tools, None, "client")
        names = {t["function"]["name"] for t in result}
        assert names == {"tool_a", "tool_b", "tool_c"}
