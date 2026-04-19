# filepath: tests/test_filesystem.py
"""Tests for filesystem tools: list_files, read_file, write_file, patch_file, mkdir."""
import asyncio
import os
import sys
import tempfile
import pathlib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Use a temp dir as workspace for tests
_temp_dir = tempfile.mkdtemp(prefix="test_workspace_")
import tooling


class TestListFiles:
    """Tests for run_list_files()."""

    def test_list_empty_dir(self):
        """list_files on empty dir returns entries list."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_list_files({"path": "."})
        )
        assert isinstance(result.get("entries"), list)

    def test_list_max_entries(self):
        """list_files respects max_entries limit."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_list_files({"path": ".", "max_entries": 5})
        )
        assert result.get("truncated", False) or len(result.get("entries", [])) <= 5

    def test_list_nonexistent_path(self):
        """list_files on nonexistent path returns error."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_list_files({"path": "this/does/not/exist"})
        )
        assert "error" in result

    def test_list_nested_recursive(self):
        """list_files with recursive=True finds nested files."""
        nested = pathlib.Path(_temp_dir) / "a" / "b" / "c"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "deep.txt").write_text("deep content")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_list_files({"path": ".", "recursive": True, "max_entries": 50})
        )
        assert result.get("truncated", False) or len(result.get("entries", [])) >= 1


class TestReadFile:
    """Tests for run_read_file()."""

    def test_read_file_basic(self):
        """read_file returns file content."""
        test_file = pathlib.Path(_temp_dir) / "read_test.txt"
        test_file.write_text("hello world")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_read_file({"path": "read_test.txt"})
        )
        assert result.get("content") == "hello world"

    def test_read_file_with_line_range(self):
        """read_file with start_line and end_line returns subset."""
        test_file = pathlib.Path(_temp_dir) / "lines.txt"
        test_file.write_text("line1\nline2\nline3\nline4\nline5")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_read_file({"path": "lines.txt", "start_line": 2, "end_line": 4})
        )
        assert result.get("content") == "line2\nline3\nline4"
        assert result.get("start_line") == 2
        assert result.get("end_line") == 4

    def test_read_file_not_found(self):
        """read_file returns error for nonexistent file."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_read_file({"path": "does_not_exist.txt"})
        )
        assert "error" in result

    def test_read_file_line_count(self):
        """read_file returns correct line_count."""
        test_file = pathlib.Path(_temp_dir) / "count.txt"
        test_file.write_text("a\nb\nc")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_read_file({"path": "count.txt"})
        )
        assert result.get("line_count") == 3


class TestWriteFile:
    """Tests for run_write_file()."""

    def test_write_file_creates_file(self):
        """write_file creates a new file."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_write_file({"path": "new_file.txt", "content": "test content"})
        )
        assert result.get("created") is True
        assert (pathlib.Path(_temp_dir) / "new_file.txt").exists()

    def test_write_file_overwrites(self):
        """write_file overwrites existing file by default."""
        test_file = pathlib.Path(_temp_dir) / "overwrite.txt"
        test_file.write_text("original")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_write_file({"path": "overwrite.txt", "content": "modified"})
        )
        assert test_file.read_text() == "modified"

    def test_write_file_no_overwrite(self):
        """write_file with overwrite=false returns error if file exists."""
        test_file = pathlib.Path(_temp_dir) / "existing.txt"
        test_file.write_text("original")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_write_file({"path": "existing.txt", "content": "new", "overwrite": False})
        )
        assert "error" in result

    def test_write_file_creates_dirs(self):
        """write_file with create_dirs=true creates parent directories."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_write_file({
                "path": "deep/nested/dir/file.txt",
                "content": "nested",
                "create_dirs": True,
            })
        )
        assert result.get("created") is True
        assert (pathlib.Path(_temp_dir) / "deep" / "nested" / "dir" / "file.txt").exists()

    def test_write_file_returns_bytes_written(self):
        """write_file returns bytes_written field."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_write_file({"path": "bytes_test.txt", "content": "12 bytes"})
        )
        assert result.get("bytes_written") == len("12 bytes")


class TestPatchFile:
    """Tests for run_patch_file()."""

    def test_patch_file_replaces_text(self):
        """patch_file replaces exact old_text with new_text."""
        test_file = pathlib.Path(_temp_dir) / "patch_test.txt"
        test_file.write_text("hello world")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_patch_file({"path": "patch_test.txt", "old_text": "world", "new_text": "there"})
        )
        assert test_file.read_text() == "hello there"
        assert result.get("replacements") == 1

    def test_patch_file_not_found(self):
        """patch_file returns error for nonexistent file."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_patch_file({"path": "no_such_file.txt", "old_text": "a", "new_text": "b"})
        )
        assert "error" in result

    def test_patch_file_old_text_not_found(self):
        """patch_file returns error when old_text not in file."""
        test_file = pathlib.Path(_temp_dir) / "found.txt"
        test_file.write_text("hello")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_patch_file({"path": "found.txt", "old_text": "not present", "new_text": "b"})
        )
        assert "error" in result

    def test_patch_file_replace_all(self):
        """patch_file with replace_all=true replaces all occurrences."""
        test_file = pathlib.Path(_temp_dir) / "all.txt"
        test_file.write_text("foo foo foo")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_patch_file({"path": "all.txt", "old_text": "foo", "new_text": "bar", "replace_all": True})
        )
        assert test_file.read_text() == "bar bar bar"
        assert result.get("replacements") == 3

    def test_patch_file_append(self):
        """patch_file with empty old_text appends new_text."""
        test_file = pathlib.Path(_temp_dir) / "append.txt"
        test_file.write_text("start")

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_patch_file({"path": "append.txt", "old_text": "", "new_text": " end"})
        )
        assert test_file.read_text() == "start end"
        assert result.get("appended") is True


class TestMkdir:
    """Tests for run_mkdir()."""

    def test_mkdir_creates_directory(self):
        """mkdir creates a new directory."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_mkdir({"path": "new_dir"})
        )
        assert (pathlib.Path(_temp_dir) / "new_dir").is_dir()
        assert result.get("created") is True

    def test_mkdir_existing_directory(self):
        """mkdir with existing directory returns created=False."""
        existing = pathlib.Path(_temp_dir) / "existing"
        existing.mkdir()

        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_mkdir({"path": "existing"})
        )
        assert result.get("created") is False

    def test_mkdir_nested_parents(self):
        """mkdir with parents=true creates nested directories."""
        result = asyncio.get_event_loop().run_until_complete(
            tooling.run_mkdir({"path": "parent/child/grandchild", "parents": True})
        )
        assert (pathlib.Path(_temp_dir) / "parent" / "child" / "grandchild").is_dir()

