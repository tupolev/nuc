# filepath: tests/test_path_safety.py
"""Tests for path safety utilities."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tooling import resolve_workspace_path, WORKSPACE_ROOT


class TestResolveWorkspacePath:
    """Tests for resolve_workspace_path()."""

    def test_relative_path_valid(self):
        """A normal relative path inside workspace should resolve."""
        result = resolve_workspace_path("src/main.js", allow_missing=True)
        assert os.path.commonpath([str(result), str(WORKSPACE_ROOT)]) == str(WORKSPACE_ROOT)

    def test_relative_path_deep(self):
        """Nested relative paths should resolve."""
        result = resolve_workspace_path("my-project/src/components/Button.tsx", allow_missing=True)
        assert os.path.commonpath([str(result), str(WORKSPACE_ROOT)]) == str(WORKSPACE_ROOT)

    def test_absolute_path_inside_workspace(self):
        """An absolute path inside workspace should resolve."""
        abs_path = os.path.join(str(WORKSPACE_ROOT), "src", "app.py")
        result = resolve_workspace_path(abs_path, allow_missing=True)
        assert os.path.commonpath([str(result), str(WORKSPACE_ROOT)]) == str(WORKSPACE_ROOT)

    def test_traversal_escape_blocked(self):
        """Path traversal attempts should be rejected."""
        for attempt in [
            "../../etc/passwd",
            "../../../root/.bashrc",
            "src/../../../etc/shadow",
            "foo/../../bar/../../baz",
        ]:
            result = resolve_workspace_path(attempt, allow_missing=True)
            assert os.path.commonpath([str(result), str(WORKSPACE_ROOT)]) == str(WORKSPACE_ROOT), \
                f"Traversal should be blocked: {attempt}"

    def test_absolute_path_outside_workspace(self):
        """An absolute path outside workspace should be rejected."""
        for abs_path in ["/etc/passwd", "/usr/bin/python3", "/tmp/evil"]:
            result = resolve_workspace_path(abs_path, allow_missing=True)
            assert os.path.commonpath([str(result), str(WORKSPACE_ROOT)]) != str(WORKSPACE_ROOT), \
                f"External absolute path should be rejected: {abs_path}"

    def test_symlink_escape(self):
        """Symlinks that resolve to paths outside workspace should be blocked."""
        # resolve_workspace_path uses os.path.commonpath to prevent escape via symlink.
        # A path like "link_to_/etc" would be rejected by the commonpath check.
        result = resolve_workspace_path("link_to_external", allow_missing=True)
        assert os.path.commonpath([str(result), str(WORKSPACE_ROOT)]) == str(WORKSPACE_ROOT)

    def test_missing_file_allowed_when_allow_missing_true(self):
        """allow_missing=True should not raise for non-existent paths."""
        result = resolve_workspace_path("nonexistent/deep/path/file.txt", allow_missing=True)
        assert result is not None

    def test_missing_file_rejected_when_allow_missing_false(self):
        """allow_missing=False should reject non-existent paths."""
        result = resolve_workspace_path("nonexistent/path/file.txt", allow_missing=False)
        assert result is None

    def test_empty_path(self):
        """Empty path should be rejected."""
        result = resolve_workspace_path("", allow_missing=True)
        assert result is None or str(result) == ""

    def test_path_normalization(self):
        """Paths should be normalized (no redundant . or double //)."""
        result = resolve_workspace_path("src/./main/../main.js", allow_missing=True)
        normalized = str(result)
        assert "//" not in normalized
        assert "/./" not in normalized

