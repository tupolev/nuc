# filepath: adapter/tests/test_session_tool_execution.py
"""
End-to-end style integration test for section 16 verification:
"Ejecutar una tool remota desde una sesion del agente."

This test does NOT require Ollama or any external service.
It validates the complete flow:
  state.create_session() -> execute_tool_call() -> append_tool_call() -> close_session()

Using a local tool (python) as the "remote" tool stand-in,
since the session/routing/execution pipeline is identical.
"""

import pytest

import state
import tooling
from tooling import execute_tool_call


@pytest.fixture(autouse=True)
def clean_session_store():
    """Ensure SESSION_STORE is clean before and after each test."""
    state.SESSION_STORE.clear()
    yield
    state.SESSION_STORE.clear()


# ---- Tests ----

@pytest.mark.asyncio
async def test_session_created_before_tool_call():
    """create_session() produces a session with a request_id."""
    session = state.create_session("test-model")
    assert "request_id" in session
    assert session["model"] == "test-model"
    assert session["active"] is True
    assert session["tool_calls"] == []


@pytest.mark.asyncio
async def test_execute_tool_call_registers_in_session():
    """execute_tool_call() with request_id appends to the session tool_calls."""
    session = state.create_session("qwen2.5-coder:7b")
    request_id = session["request_id"]

    result = await execute_tool_call(
        "python",
        {"code": "print(1 + 1)"},
        request_id=request_id,
    )

    assert "error" not in result
    assert result["returncode"] == 0
    assert "1" in result["stdout"] or "2" in result["stdout"]

    # Verify session was updated
    updated = state.get_session(request_id)
    assert updated is not None
    assert len(updated["tool_calls"]) == 1
    assert updated["tool_calls"][0]["name"] == "python"
    assert updated["tool_calls"][0]["args"]["code"] == "print(1 + 1)"
    assert "result" in updated["tool_calls"][0]
    assert updated["tool_calls"][0]["result"]["returncode"] == 0


@pytest.mark.asyncio
async def test_execute_tool_call_multiple_tools_in_session():
    """Multiple tool calls in the same session are all recorded."""
    session = state.create_session("qwen2.5-coder:7b")
    request_id = session["request_id"]

    await execute_tool_call("python", {"code": "x = 2"}, request_id=request_id)
    await execute_tool_call("python", {"code": "print(x * 3)"}, request_id=request_id)

    updated = state.get_session(request_id)
    assert len(updated["tool_calls"]) == 2
    assert updated["tool_calls"][0]["name"] == "python"
    assert updated["tool_calls"][1]["name"] == "python"


@pytest.mark.asyncio
async def test_session_close_registers_final_result():
    """close_session() marks the session inactive and stores final_result."""
    session = state.create_session("qwen2.5-coder:7b")
    request_id = session["request_id"]

    await execute_tool_call("python", {"code": "print('done')"}, request_id=request_id)
    state.close_session(request_id, final_result="Build passed.")

    updated = state.get_session(request_id)
    assert updated["active"] is False
    assert updated["final_result"] == "Build passed."


@pytest.mark.asyncio
async def test_session_close_with_error():
    """close_session() with error stores the error and marks inactive."""
    session = state.create_session("qwen2.5-coder:7b")
    request_id = session["request_id"]

    state.close_session(request_id, error="model timeout")

    updated = state.get_session(request_id)
    assert updated["active"] is False
    assert updated["error"] == "model timeout"


@pytest.mark.asyncio
async def test_execute_tool_call_without_session_id():
    """execute_tool_call() without request_id does NOT crash (session is optional)."""
    result = await execute_tool_call("python", {"code": "print('no session')"})
    assert "error" not in result
    assert result["returncode"] == 0


@pytest.mark.asyncio
async def test_session_with_unknown_tool_returns_error():
    """execute_tool_call() for unknown tool name returns an error."""
    session = state.create_session("qwen2.5-coder:7b")
    request_id = session["request_id"]

    result = await execute_tool_call(
        "nonexistent_tool_xyz",
        {"arg": "value"},
        request_id=request_id,
    )

    assert "error" in result
    assert "not found" in result["error"].lower() or "unknown" in result["error"].lower()

    # Error is also recorded in session
    updated = state.get_session(request_id)
    assert len(updated["tool_calls"]) == 1
    assert "error" in updated["tool_calls"][0]["result"]


@pytest.mark.asyncio
async def test_session_get_returns_none_for_unknown_id():
    """get_session() returns None for unknown request_id."""
    assert state.get_session("nonexistent-id-xyz") is None


@pytest.mark.asyncio
async def test_session_ttl_still_allows_tool_calls():
    """Sessions near TTL boundary can still record tool calls."""
    session = state.create_session("test-model")
    request_id = session["request_id"]

    # Simulate session is old by directly setting created_at
    import time
    state.SESSION_STORE[request_id]["created_at"] = time.time() - 3500  # ~58 min ago

    result = await execute_tool_call("python", {"code": "print('still works')"}, request_id=request_id)
    assert "error" not in result

    updated = state.get_session(request_id)
    assert updated is not None
    assert len(updated["tool_calls"]) == 1
