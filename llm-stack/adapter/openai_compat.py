import json
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import httpx

from config import AUTO_ENABLE_LOCAL_TOOLS, OLLAMA_URL, TOOL_EXECUTION_MODE, TOOL_MAX_ITERATIONS
from state import METRICS, bump_metric_dict, bump_tool_status_metric
from tooling import (
    TOOL_REGISTRY,
    build_all_tool_specs,
    build_tool_spec,
    execute_tool_call,
    safe_json_dumps,
)


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


def normalize_local_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
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


def normalize_passthrough_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not tools:
        return []

    normalized = []
    seen = set()

    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue

        fn = tool.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue

        normalized.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(fn.get("description", "") or ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                },
            }
        )
        seen.add(name)

    return normalized


def build_effective_tools(
    requested_tools: Any,
    legacy_functions: Any,
    execution_mode: str,
    allow_auto_server_tools: bool = False,
) -> List[Dict[str, Any]]:
    candidate_tools = requested_tools or []
    if not candidate_tools and legacy_functions:
        candidate_tools = convert_legacy_functions_to_tools(legacy_functions)

    if execution_mode == "client":
        return normalize_passthrough_openai_tools(candidate_tools)

    normalized = normalize_local_openai_tools(candidate_tools)
    if normalized:
        return normalized

    if execution_mode == "server" and allow_auto_server_tools and AUTO_ENABLE_LOCAL_TOOLS:
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
        "Do not invoke skills, slash commands, plugins, MCP commands, or any external capability unless it was explicitly provided as a tool in this request. "
        "Never emit lines like 'Skill \"...\"', '/command', or similar pseudo-tool syntax. "
        "If you need to act, use only the provided function tools. "
        "Do not invent tool results. After tool results are provided, continue and produce the final answer.\n"
        "IMPORTANT — file operations: if the user asks you to create, modify, or save a file, "
        "you MUST call the write_file or patch_file tool to actually perform the operation. "
        "Describing a plan to write a file is NOT the same as writing it. "
        "ALWAYS execute the tool call that performs the action the user requested."
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
        if role not in {"system", "user", "assistant", "tool", "function"}:
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
        elif role == "assistant" and isinstance(message.get("function_call"), dict):
            normalized_item["function_call"] = message["function_call"]

        if role == "tool":
            if message.get("name"):
                normalized_item["name"] = message["name"]
            if message.get("tool_name"):
                normalized_item["tool_name"] = message["tool_name"]
            if message.get("tool_call_id"):
                normalized_item["tool_call_id"] = message["tool_call_id"]
        elif role == "function":
            normalized_item["role"] = "tool"
            if message.get("name"):
                normalized_item["tool_name"] = message["name"]

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
        elif role == "assistant" and isinstance(message.get("function_call"), dict):
            fn = message.get("function_call") or {}
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            item["tool_calls"] = [{"function": {"name": fn.get("name", ""), "arguments": args}}]

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
    if not isinstance(function_payload, dict) and isinstance(item.get("function_call"), dict):
        function_payload = item.get("function_call")

    if isinstance(function_payload, dict):
        name = choose_tool_name(function_payload.get("name"), available_tools, tool_choice)
        arguments = (
            function_payload.get("arguments")
            if "arguments" in function_payload
            else function_payload.get("input", {})
        )
    else:
        requested_name = item.get("name")
        if requested_name is None:
            requested_name = item.get("tool_name")
        if requested_name is None:
            requested_name = item.get("tool")
        name = choose_tool_name(requested_name, available_tools, tool_choice)
        arguments = item.get("arguments")
        if arguments is None:
            arguments = item.get("input")
        if arguments is None:
            arguments = item.get("params", {})

    if not name:
        return None

    return {
        "id": item.get("id") or f"call_{uuid.uuid4().hex}",
        "function": {
            "name": name,
            "arguments": arguments,
        },
    }


def iter_json_candidates(text: str) -> List[Any]:
    decoder = json.JSONDecoder()
    payloads: List[Any] = []
    starts = []

    for index, char in enumerate(text):
        if char in "[{":
            starts.append(index)

    for start in starts:
        try:
            payload, _ = decoder.raw_decode(text, start)
        except Exception:
            continue
        payloads.append(payload)

    return payloads


def extract_tool_calls_from_content(
    content: str,
    available_tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    if not content or not available_tools:
        return []

    text = strip_wrapping_code_fence(content)
    payloads: List[Any] = []

    try:
        payloads = [json.loads(text)]
    except Exception:
        payloads = iter_json_candidates(text)
        if not payloads:
            return []

    candidate_items: List[Dict[str, Any]] = []
    for payload in payloads:
        if isinstance(payload, dict) and isinstance(payload.get("tool_calls"), list):
            candidate_items.extend(item for item in payload["tool_calls"] if isinstance(item, dict))
        elif isinstance(payload, dict):
            candidate_items.append(payload)
        elif isinstance(payload, list):
            candidate_items.extend(item for item in payload if isinstance(item, dict))

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

    raw_function_call = raw_message.get("function_call")
    if isinstance(raw_function_call, dict):
        normalized = coerce_fallback_tool_call({"function_call": raw_function_call}, available_tools, tool_choice)
        return [normalized] if normalized else []

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


def parse_tool_message_content(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if message.get("role") != "tool":
        return None

    content = message.get("content", "")
    if not isinstance(content, str) or not content:
        return None

    try:
        parsed = json.loads(content)
    except Exception:
        return None

    if isinstance(parsed, dict):
        return parsed
    return None


def get_latest_build_exec_result(history: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for message in reversed(history):
        if message.get("role") != "tool" or message.get("tool_name") != "exec_command":
            continue

        payload = parse_tool_message_content(message)
        if not payload:
            continue

        command = payload.get("command")
        command_text = " ".join(command) if isinstance(command, list) else str(command or "")
        lowered = command_text.lower()
        if "build" not in lowered:
            continue

        return {
            "command": command_text,
            "cwd": payload.get("cwd", "."),
            "returncode": payload.get("returncode"),
            "stdout": str(payload.get("stdout", "") or ""),
            "stderr": str(payload.get("stderr", "") or ""),
            "timed_out": bool(payload.get("timed_out", False)),
        }

    return None


def reconcile_final_message_with_tool_results(
    final_message: Dict[str, Any],
    history: List[Dict[str, Any]],
) -> Dict[str, Any]:
    content = str(final_message.get("content", "") or "")
    latest_build = get_latest_build_exec_result(history)
    if not latest_build:
        return final_message

    lowered = content.lower()
    build_succeeded = latest_build.get("returncode") == 0 and not latest_build.get("timed_out")
    build_failed = latest_build.get("returncode") not in {None, 0} or latest_build.get("timed_out")

    failure_markers = ("fail", "failed", "error", "did not pass", "unsuccessful")
    success_markers = ("passed", "succeeded", "successful", "built", "build passed", "build succeeded")
    says_failure = any(marker in lowered for marker in failure_markers)
    says_success = any(marker in lowered for marker in success_markers)

    if build_succeeded and says_failure:
        note = (
            f" Verified note: the latest build command `{latest_build['command']}` in "
            f"`{latest_build['cwd']}` exited with code 0."
        )
        final_message["content"] = (content + note).strip()
        return final_message

    if build_failed and says_success:
        note = (
            f" Verified note: the latest build command `{latest_build['command']}` in "
            f"`{latest_build['cwd']}` did not succeed."
        )
        final_message["content"] = (content + note).strip()
        return final_message

    return final_message


async def run_chat_with_native_tools(
    model: str,
    messages: List[Dict[str, Any]],
    tools: List[Dict[str, Any]],
    tool_choice: Optional[Dict[str, Any]] = None,
    execution_mode: str = "server",
    max_iterations: int = TOOL_MAX_ITERATIONS,
    request_id: Optional[str] = None,
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
                final_message = reconcile_final_message_with_tool_results(final_message, history)
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
                    tool_result = await execute_tool_call(tool_name, tool_args, request_id)

                history.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call["id"],
                        "tool_name": tool_name,
                        "content": safe_json_dumps(tool_result),
                    }
                )

        raise RuntimeError("max tool iterations reached")
