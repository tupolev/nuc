# filepath: tests/test_exec_command.py
"""Tests for exec_command tool."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import EXEC_COMMAND_ALLOWLIST


class TestExecAllowlist:
    """Tests for the exec_command allowlist (EXEC_COMMAND_ALLOWLIST)."""

    def test_critical_commands_in_allowlist(self):
        """Critical development commands should be in the allowlist."""
        critical = ["node", "npm", "python3", "pip", "php", "composer", "git", "bash", "sh"]
        for cmd in critical:
            assert cmd in EXEC_COMMAND_ALLOWLIST, f"{cmd} should be in EXEC_COMMAND_ALLOWLIST"

    def test_php_tools_in_allowlist(self):
        """PHP development tools should be in the allowlist."""
        php_tools = ["php", "composer", "phpunit", "phpcs", "phpstan", "php-cs-fixer", "artisan"]
        for tool in php_tools:
            assert tool in EXEC_COMMAND_ALLOWLIST, f"{tool} should be in EXEC_COMMAND_ALLOWLIST"

    def test_framework_commands_in_allowlist(self):
        """Framework commands like bin/console and apache2ctl should be allowed."""
        framework = ["bin/console", "apache2ctl", "nginx"]
        for cmd in framework:
            assert cmd in EXEC_COMMAND_ALLOWLIST, f"{cmd} should be in EXEC_COMMAND_ALLOWLIST"

    def test_dangerous_commands_not_in_allowlist(self):
        """Dangerous commands should NOT be in the allowlist."""
        dangerous = ["rm", "mkfs", "dd", ":(){:|:&};:", "forkbomb", "curl", "wget", "nc"]
        for cmd in dangerous:
            assert cmd not in EXEC_COMMAND_ALLOWLIST, f"{cmd} should NOT be in EXEC_COMMAND_ALLOWLIST"

    def test_allowlist_is_iterable(self):
        """Allowlist should be iterable and not empty."""
        allowlist = list(EXEC_COMMAND_ALLOWLIST)
        assert len(allowlist) > 0
        assert all(isinstance(cmd, str) for cmd in allowlist)


class TestExecCommandValidation:
    """Tests for exec_command argument validation behavior.

    Note: Full exec_command integration tests require the adapter runtime.
    These tests cover the config and known-constant validation layer.
    """

    def test_run_exec_command_import(self):
        """run_exec_command should be importable from tooling."""
        from tooling import run_exec_command
        import inspect
        assert inspect.iscoroutinefunction(run_exec_command)

    def test_empty_command_returns_error(self):
        """run_exec_command should return error for empty command."""
        import asyncio
        from tooling import run_exec_command
        result = asyncio.get_event_loop().run_until_complete(
            run_exec_command({"command": "", "cwd": "."})
        )
        assert "error" in result

    def test_command_missing_required_fields(self):
        """run_exec_command should return error when required fields are missing."""
        import asyncio
        from tooling import run_exec_command
        result = asyncio.get_event_loop().run_until_complete(
            run_exec_command({})
        )
        assert "error" in result
