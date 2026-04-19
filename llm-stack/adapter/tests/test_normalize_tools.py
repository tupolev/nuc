# filepath: tests/test_normalize_tools.py
"""Tests for normalize_openai_tools functions."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openai_compat import (
    normalize_local_openai_tools,
    normalize_passthrough_openai_tools,
    TOOL_REGISTRY,
)


class TestNormalizeLocalOpenaiTools:
    """Tests for normalize_local_openai_tools()."""

    def test_passes_valid_local_tools(self):
        """Tools that exist in TOOL_REGISTRY should pass."""
        tools = [
            {"type": "function", "function": {"name": "read_file", "description": "Read a file", "parameters": {}}},
            {"type": "function", "function": {"name": "write_file", "description": "Write a file", "parameters": {}}},
        ]
        result = normalize_local_openai_tools(tools)
        names = {t["function"]["name"] for t in result}
        assert "read_file" in names
        assert "write_file" in names

    def test_rejects_unknown_tools(self):
        """Tools not in TOOL_REGISTRY should be rejected."""
        tools = [
            {"type": "function", "function": {"name": "unknown_tool", "description": "Fake", "parameters": {}}},
            {"type": "function", "function": {"name": "another_fake", "description": "Also fake", "parameters": {}}},
        ]
        result = normalize_local_openai_tools(tools)
        assert len(result) == 0

    def test_rejects_duplicates(self):
        """Duplicate tool names should be deduplicated (first wins)."""
        tools = [
            {"type": "function", "function": {"name": "read_file", "description": "First", "parameters": {}}},
            {"type": "function", "function": {"name": "read_file", "description": "Second", "parameters": {}}},
        ]
        result = normalize_local_openai_tools(tools)
        assert len(result) == 1

    def test_rejects_non_function_type(self):
        """Non-function tools should be skipped."""
        tools = [
            {"type": "unknown", "function": {"name": "read_file", "description": "X", "parameters": {}}},
            {"type": "function", "function": {"name": "write_file", "description": "Y", "parameters": {}}},
        ]
        result = normalize_local_openai_tools(tools)
        names = {t["function"]["name"] for t in result}
        assert "write_file" in names
        assert "read_file" not in names

    def test_rejects_missing_name(self):
        """Tools without name should be skipped."""
        tools = [
            {"type": "function", "function": {"description": "No name", "parameters": {}}},
            {"type": "function", "function": {"name": "", "description": "Empty name", "parameters": {}}},
        ]
        result = normalize_local_openai_tools(tools)
        assert len(result) == 0

    def test_empty_input(self):
        """Empty list should return empty list."""
        assert normalize_local_openai_tools([]) == []
        assert normalize_local_openai_tools(None) == []


class TestNormalizePassthroughOpenaiTools:
    """Tests for normalize_passthrough_openai_tools()."""

    def test_passes_external_tools(self):
        """Tools not in TOOL_REGISTRY should pass through."""
        tools = [
            {"type": "function", "function": {"name": "my_custom_tool", "description": "Custom", "parameters": {"type": "object"}}},
        ]
        result = normalize_passthrough_openai_tools(tools)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "my_custom_tool"

    def test_rejects_local_collisions(self):
        """Tools whose name matches a local tool should be filtered."""
        tools = [
            {"type": "function", "function": {"name": "read_file", "description": "Should be blocked", "parameters": {}}},
            {"type": "function", "function": {"name": "exec_command", "description": "Should also be blocked", "parameters": {}}},
            {"type": "function", "function": {"name": "my_custom_tool", "description": "Should pass", "parameters": {}}},
        ]
        result = normalize_passthrough_openai_tools(tools)
        names = {t["function"]["name"] for t in result}
        assert "read_file" not in names
        assert "exec_command" not in names
        assert "my_custom_tool" in names

    def test_rejects_all_locals(self):
        """All local tool names should be blocked regardless of description."""
        all_local = [
            {"type": "function", "function": {"name": name, "description": f"Fake {name}", "parameters": {}}}
            for name in list(TOOL_REGISTRY.keys())[:5]
        ]
        result = normalize_passthrough_openai_tools(all_local)
        assert len(result) == 0

    def test_rejects_duplicates(self):
        """Duplicate names in passthrough should be deduplicated."""
        tools = [
            {"type": "function", "function": {"name": "tool_a", "description": "First", "parameters": {}}},
            {"type": "function", "function": {"name": "tool_a", "description": "Second", "parameters": {}}},
        ]
        result = normalize_passthrough_openai_tools(tools)
        assert len(result) == 1

    def test_rejects_non_function_type(self):
        """Non-function tools should be skipped."""
        tools = [
            {"type": "other", "function": {"name": "custom_tool", "description": "X", "parameters": {}}},
            {"type": "function", "function": {"name": "custom_tool", "description": "Y", "parameters": {}}},
        ]
        result = normalize_passthrough_openai_tools(tools)
        assert len(result) == 1

    def test_empty_input(self):
        """Empty list should return empty list."""
        assert normalize_passthrough_openai_tools([]) == []
        assert normalize_passthrough_openai_tools(None) == []

    def test_preserves_description_and_parameters(self):
        """Passthrough should keep description and parameters intact."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "external_tool",
                    "description": "Does something",
                    "parameters": {"type": "object", "properties": {"arg1": {"type": "string"}}},
                },
            }
        ]
        result = normalize_passthrough_openai_tools(tools)
        assert len(result) == 1
        assert result[0]["function"]["description"] == "Does something"
        assert result[0]["function"]["parameters"]["properties"]["arg1"]["type"] == "string"
