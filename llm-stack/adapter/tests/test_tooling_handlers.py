# filepath: adapter/tests/test_tooling_handlers.py
"""
Unit tests for run_python, run_fetch_url, run_http_request, run_sqlite_query.
"""

import os
import sqlite3
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Import handlers directly from tooling module
import tooling


# =============================================================================
# run_python
# =============================================================================

class TestRunPython:
    """Tests for run_python handler."""

    def test_python_returns_error_when_code_empty(self):
        """Empty code returns error."""
        result = tooling.run_python_sync({"code": ""})
        assert result["error"] == "code is required"

    def test_python_returns_error_when_code_missing(self):
        """Missing code returns error."""
        result = tooling.run_python_sync({})
        assert result["error"] == "code is required"

    def test_python_returns_error_when_code_too_long(self):
        """Code exceeding max length returns error."""
        long_code = "x" * (tooling.PYTHON_CODE_MAX_LEN + 1)
        result = tooling.run_python_sync({"code": long_code})
        assert "code too long" in result["error"]

    def test_python_executes_print_statement(self):
        """print('hello') returns returncode 0 and 'hello' in stdout."""
        result = tooling.run_python_sync({"code": "print('hello world')"})
        assert result["returncode"] == 0
        assert "hello world" in result["stdout"]
        assert result["stderr"] == ""

    def test_python_executes_arithmetic(self):
        """Arithmetic expression returns correct result."""
        result = tooling.run_python_sync({"code": "print(2 + 2)"})
        assert result["returncode"] == 0
        assert "4" in result["stdout"]

    def test_python_returns_stderr_on_error(self):
        """Runtime error returns non-zero and stderr."""
        result = tooling.run_python_sync({"code": "raise ValueError('test error')"})
        assert result["returncode"] != 0
        assert "ValueError" in result["stderr"]

    def test_python_returns_syntax_error(self):
        """Syntax error returns non-zero returncode."""
        result = tooling.run_python_sync({"code": "print("})
        assert result["returncode"] != 0
        assert result["stderr"] != ""

    def test_python_stdout_truncated_at_max_len(self):
        """Very long stdout is truncated."""
        huge = "x" * (tooling.TOOL_OUTPUT_MAX_LEN + 1000)
        result = tooling.run_python_sync({"code": f"print('{huge}')"})
        assert len(result["stdout"]) <= tooling.TOOL_OUTPUT_MAX_LEN

    def test_python_import_error(self):
        """Importing nonexistent module returns error in stderr."""
        result = tooling.run_python_sync({"code": "import nonexistent_module_12345"})
        assert result["returncode"] != 0
        assert "ModuleNotFoundError" in result["stderr"]

    def test_python_none_code(self):
        """None as code returns error."""
        result = tooling.run_python_sync({"code": None})
        assert result["error"] == "code is required"


# =============================================================================
# run_fetch_url
# =============================================================================

class TestRunFetchUrl:
    """Tests for run_fetch_url handler."""

    @pytest.mark.asyncio
    async def test_fetch_url_returns_error_when_url_missing(self):
        """Missing url returns error."""
        result = await tooling.run_fetch_url({})
        assert result["error"] == "url is required"

    @pytest.mark.asyncio
    async def test_fetch_url_returns_error_when_url_empty(self):
        """Empty url returns error."""
        result = await tooling.run_fetch_url({"url": "  "})
        assert result["error"] == "url is required"

    @pytest.mark.asyncio
    async def test_fetch_url_returns_error_for_invalid_scheme(self):
        """URL without http/https returns error."""
        result = await tooling.run_fetch_url({"url": "ftp://example.com"})
        assert result["error"] == "url must start with http:// or https://"

    @pytest.mark.asyncio
    async def test_fetch_url_returns_error_for_local_path(self):
        """Local file path returns error."""
        result = await tooling.run_fetch_url({"url": "/etc/passwd"})
        assert result["error"] == "url must start with http:// or https://"

    @pytest.mark.asyncio
    async def test_fetch_url_text_mode(self):
        """Text mode returns status_code, content_type, text."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain; charset=utf-8"}
        mock_response.text = "Hello World"

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_fetch_url({"url": "https://example.com"})

        assert result["url"] == "https://example.com"
        assert result["status_code"] == 200
        assert result["content_type"] == "text/plain; charset=utf-8"
        assert result["text"] == "Hello World"

    @pytest.mark.asyncio
    async def test_fetch_url_json_mode_returns_json_payload(self):
        """mode=json returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"key": "value", "count": 42}

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_fetch_url({"url": "https://api.example.com/data", "mode": "json"})

        assert result["status_code"] == 200
        assert result["json"] == {"key": "value", "count": 42}

    @pytest.mark.asyncio
    async def test_fetch_url_json_mode_returns_error_on_invalid_json(self):
        """mode=json returns error when response is not valid JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.json.side_effect = ValueError("not JSON")

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_fetch_url({"url": "https://example.com", "mode": "json"})

        assert "error" in result
        assert "not valid JSON" in result["error"]

    @pytest.mark.asyncio
    async def test_fetch_url_truncates_text_at_max_len(self):
        """Text response exceeding max is truncated."""
        huge_text = "x" * (tooling.TOOL_OUTPUT_MAX_LEN + 500)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = huge_text

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_fetch_url({"url": "https://example.com"})

        assert len(result["text"]) <= tooling.TOOL_OUTPUT_MAX_LEN

    @pytest.mark.asyncio
    async def test_fetch_url_uses_configured_timeout(self):
        """Timeout is passed through to httpx client."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = "ok"

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.get = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            await tooling.run_fetch_url({"url": "https://example.com", "timeout_seconds": 45})

        # Check timeout was set (should be max(3, min(45, 60)) = 45)
        call_kwargs = mock_instance.get.call_args
        created_timeout = call_kwargs.kwargs.get("timeout") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else None
        # The httpx client is created with the timeout
        assert MockClient.call_args[1]["timeout"] == 45


# =============================================================================
# run_http_request
# =============================================================================

class TestRunHttpRequest:
    """Tests for run_http_request handler."""

    @pytest.mark.asyncio
    async def test_http_request_returns_error_when_url_missing(self):
        """Missing url returns error."""
        result = await tooling.run_http_request({})
        assert result["error"] == "url is required"

    @pytest.mark.asyncio
    async def test_http_request_returns_error_for_invalid_scheme(self):
        """Non-http URL returns error."""
        result = await tooling.run_http_request({"url": "file:///etc/passwd"})
        assert result["error"] == "url must start with http:// or https://"

    @pytest.mark.asyncio
    async def test_http_request_returns_error_for_unsupported_method(self):
        """Unsupported HTTP method returns error."""
        result = await tooling.run_http_request({"url": "https://example.com", "method": "LINK"})
        assert result["error"] == "unsupported method"

    @pytest.mark.asyncio
    async def test_http_request_returns_error_when_headers_not_dict(self):
        """Non-dict headers returns error."""
        result = await tooling.run_http_request({
            "url": "https://example.com",
            "headers": "not-a-dict",
        })
        assert result["error"] == "headers must be an object"

    @pytest.mark.asyncio
    async def test_http_request_get_returns_text(self):
        """GET request returns status, content_type, text."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '{"ok": true}'

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.request = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_http_request({"url": "https://example.com/api"})

        assert result["url"] == "https://example.com/api"
        assert result["method"] == "GET"
        assert result["status_code"] == 200
        assert result["content_type"] == "application/json"

    @pytest.mark.asyncio
    async def test_http_request_post_with_json_body(self):
        """POST with dict body sends JSON and returns parsed response."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"id": 1, "name": "test"}

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.request = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_http_request({
                "url": "https://example.com/items",
                "method": "POST",
                "body": {"name": "test"},
            })

        assert result["status_code"] == 201
        assert result["json"] == {"id": 1, "name": "test"}
        # Verify json= was passed (body was dict)
        call_args = mock_instance.request.call_args
        assert call_args.kwargs.get("json") == {"name": "test"}

    @pytest.mark.asyncio
    async def test_http_request_delete(self):
        """DELETE request works."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = ""

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.request = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_http_request({
                "url": "https://example.com/items/5",
                "method": "DELETE",
            })

        assert result["status_code"] == 204

    @pytest.mark.asyncio
    async def test_http_request_json_mode_returns_parsed_json(self):
        """mode=json returns parsed JSON."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.json.return_value = {"result": "success"}

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.request = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_http_request({
                "url": "https://example.com/api",
                "mode": "json",
            })

        assert result["json"] == {"result": "success"}

    @pytest.mark.asyncio
    async def test_http_request_custom_headers(self):
        """Custom headers are passed through."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = "ok"

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.request = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            await tooling.run_http_request({
                "url": "https://example.com",
                "headers": {"Authorization": "Bearer token123"},
            })

            call_args = mock_instance.request.call_args
            assert call_args.kwargs.get("headers") == {"Authorization": "Bearer token123"}

    @pytest.mark.asyncio
    async def test_http_request_truncates_large_response(self):
        """Large responses are truncated at HTTP_MAX_BYTES."""
        huge = "x" * (tooling.HTTP_MAX_BYTES + 1000)
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = huge

        with patch("httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.__aenter__.return_value = mock_instance
            mock_instance.__aexit__.return_value = None
            mock_instance.request = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_instance

            result = await tooling.run_http_request({"url": "https://example.com/large"})

        # Text is first truncated to HTTP_MAX_BYTES then to TOOL_OUTPUT_MAX_LEN
        assert len(result["text"]) <= tooling.TOOL_OUTPUT_MAX_LEN


# =============================================================================
# run_sqlite_query
# =============================================================================

class TestRunSqliteQuery:
    """Tests for run_sqlite_query handler."""

    def test_sqlite_query_returns_error_when_query_missing(self):
        """Missing query returns error."""
        result = tooling.run_sqlite_query_sync({})
        assert result["error"] == "query is required"

    def test_sqlite_query_returns_error_when_query_empty(self):
        """Empty query returns error."""
        result = tooling.run_sqlite_query_sync({"query": "   "})
        assert result["error"] == "query is required"

    def test_sqlite_query_returns_error_on_multiple_statements(self):
        """Multiple SQL statements return error."""
        result = tooling.run_sqlite_query_sync({"query": "SELECT 1; SELECT 2"})
        assert result["error"] == "multiple SQL statements are not allowed"

    def test_sqlite_query_allows_single_select(self):
        """SELECT query returns rows."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE test(id INTEGER PRIMARY KEY, name TEXT)")
            conn.execute("INSERT INTO test(name) VALUES ('Alice')")
            conn.execute("INSERT INTO test(name) VALUES ('Bob')")
            conn.commit()
            conn.close()

            result = tooling.run_sqlite_query_sync({"db_path": f.name, "query": "SELECT * FROM test"})

        assert result["row_count"] == 2
        assert result["columns"] == ["id", "name"]
        assert result["rows"][0] == {"id": 1, "name": "Alice"}
        assert result["rows"][1] == {"id": 2, "name": "Bob"}

    def test_sqlite_query_readonly_blocks_write(self):
        """readonly=true blocks INSERT/UPDATE/DELETE."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            result = tooling.run_sqlite_query_sync({
                "db_path": f.name,
                "query": "INSERT INTO test VALUES (1)",
                "readonly": "true",
            })
            assert result["error"] == "readonly mode only allows select/pragma/with/explain queries"

    def test_sqlite_query_readonly_allows_pragma(self):
        """readonly=true allows PRAGMA queries."""
        result = tooling.run_sqlite_query_sync({"query": "PRAGMA database_list", "readonly": "true"})
        assert "error" not in result
        assert "columns" in result

    def test_sqlite_query_allows_write_when_readonly_false(self):
        """readonly=false allows INSERT."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE t(v TEXT)")
            conn.commit()
            conn.close()

            result = tooling.run_sqlite_query_sync({
                "db_path": f.name,
                "query": "INSERT INTO t VALUES ('hello')",
                "readonly": "false",
            })

        assert "error" not in result
        assert result["row_count"] == 0  # INSERT has no result rows

    def test_sqlite_query_respects_max_rows(self):
        """Results are limited to SQLITE_QUERY_MAX_ROWS."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE nums(n INTEGER)")
            for i in range(300):
                conn.execute("INSERT INTO nums(n) VALUES (?)", (i,))
            conn.commit()
            conn.close()

            result = tooling.run_sqlite_query_sync({
                "db_path": f.name,
                "query": "SELECT n FROM nums",
            })

        assert result["row_count"] == tooling.SQLITE_QUERY_MAX_ROWS
        assert len(result["rows"]) == tooling.SQLITE_QUERY_MAX_ROWS

    def test_sqlite_query_explain_query(self):
        """EXPLAIN queries are allowed in readonly mode."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            result = tooling.run_sqlite_query_sync({
                "db_path": f.name,
                "query": "EXPLAIN QUERY PLAN SELECT 1",
                "readonly": "true",
            })
            assert "error" not in result

    def test_sqlite_query_with_clause(self):
        """WITH (recursive) queries are allowed in readonly mode."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            result = tooling.run_sqlite_query_sync({
                "db_path": f.name,
                "query": "WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM cnt WHERE x<5) SELECT x FROM cnt",
                "readonly": "true",
            })
            assert "error" not in result
            assert result["row_count"] == 5

    def test_sqlite_query_blocks_update_in_readonly(self):
        """UPDATE is blocked in readonly mode."""
        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            result = tooling.run_sqlite_query_sync({
                "db_path": f.name,
                "query": "UPDATE test SET name='x' WHERE 1=1",
                "readonly": "true",
            })
            assert "error" in result
            assert "readonly" in result["error"].lower()

    def test_sqlite_query_nonexistent_db(self):
        """Query on nonexistent database returns error."""
        result = tooling.run_sqlite_query_sync({
            "db_path": "/nonexistent/path/to/db.sqlite",
            "query": "SELECT 1",
        })
        assert "error" in result
