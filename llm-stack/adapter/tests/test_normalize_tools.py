# filepath: tests/test_normalize_tools.py
"""Tests for normalize_openai_tools functions."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openai_compat import (
    extract_textual_json_tool_call,
    normalize_local_openai_tools,
    normalize_passthrough_openai_tools,
    normalize_tool_arguments,
    to_openai_messages,
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


class TestNormalizeToolArguments:
    """Tests for tolerant model-emitted tool argument normalization."""

    def test_bash_commands_list_becomes_required_command_string(self):
        result = normalize_tool_arguments(
            "bash",
            {"commands": ["gcc weather.c -o weather -lcurl", "./weather"]},
        )

        assert result == {"command": "gcc weather.c -o weather -lcurl && ./weather"}

    def test_bash_command_list_becomes_command_string(self):
        result = normalize_tool_arguments(
            "bash",
            {"command": ["git status", "git diff"]},
        )

        assert result == {"command": "git status && git diff"}

    def test_bash_cmd_alias_becomes_command(self):
        result = normalize_tool_arguments("bash", {"cmd": "./weather Madrid"})

        assert result == {"command": "./weather Madrid"}


class TestToOpenaiMessages:
    def test_preserves_tool_call_protocol_fields(self):
        tool_call = {
            "id": "call_weather",
            "type": "function",
            "function": {"name": "bash", "arguments": '{"command":"./weather"}'},
        }
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [tool_call]},
            {
                "role": "tool",
                "tool_call_id": "call_weather",
                "tool_name": "bash",
                "content": "Berlin: 15 C",
            },
        ]

        result = to_openai_messages(messages)

        assert result[0]["tool_calls"] == [tool_call]
        assert result[1]["tool_call_id"] == "call_weather"
        assert result[1]["name"] == "bash"


class TestExtractTextualJsonToolCall:
    """Tests for textual JSON write extraction."""

    @staticmethod
    def requested_write_tool():
        return [
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "filePath": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["filePath", "content"],
                    },
                },
            }
        ]

    @staticmethod
    def behavior_flags():
        return {
            "enable_pseudo_tool_extraction": True,
            "enable_textual_json_tool_extraction": True,
            "enable_placeholder_path_cleanup": True,
            "enable_schema_description_unwrap": True,
        }

    def test_extracts_valid_write_call_from_textual_json(self):
        result = extract_textual_json_tool_call(
            '{"name":"write","arguments":{"filePath":"list.cpp","content":"int main() { return 0; }"}}',
            requested_tools=self.requested_write_tool(),
            behavior_flags=self.behavior_flags(),
        )

        assert len(result) == 1
        assert result[0]["function"]["name"] == "write"
        assert '"filePath": "list.cpp"' in result[0]["function"]["arguments"]
        assert "int main() { return 0; }" in result[0]["function"]["arguments"]

    def test_rejects_placeholder_write_call_from_textual_json(self):
        result = extract_textual_json_tool_call(
            '{"name":"write","arguments":{"filePath":"/path/to/file","content":"The content to write to the file."}}',
            requested_tools=self.requested_write_tool(),
            behavior_flags=self.behavior_flags(),
        )

        assert result == []
