"""Tests for VirtualFS bulk operations (write_many, remove_many)."""

import pytest

from monkeyfs import VirtualFS


class TestBulkOperations:
    """Test VirtualFS bulk operations."""

    def test_write_many_basic(self):
        """Test writing multiple files at once."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content 1",
            "file2.txt": b"content 2",
            "dir/file3.txt": b"content 3",
        }

        vfs.write_many(files)

        # All files should exist
        assert vfs.read("file1.txt") == b"content 1"
        assert vfs.read("file2.txt") == b"content 2"
        assert vfs.read("dir/file3.txt") == b"content 3"

    def test_write_many_validates_all_bytes(self):
        """Test that write_many validates all content is bytes."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content 1",
            "file2.txt": "not bytes",  # Invalid!
        }

        with pytest.raises(TypeError, match="Expected bytes for 'file2.txt'"):
            vfs.write_many(files)

        # No files should be written (validation happens first)
        assert not vfs.exists("file1.txt")

    def test_write_many_with_dict_state(self):
        """Test that write_many works with dict state (no snapshot method)."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content 1",
            "file2.txt": b"content 2",
        }

        # Should not raise - dict doesn't have snapshot()
        vfs.write_many(files)

        assert vfs.exists("file1.txt")
        assert vfs.exists("file2.txt")

    def test_remove_many_basic(self):
        """Test removing multiple files at once."""
        vfs = VirtualFS({})

        # Create files
        vfs.write("file1.txt", b"content 1")
        vfs.write("file2.txt", b"content 2")
        vfs.write("file3.txt", b"content 3")

        # Remove two of them
        vfs.remove_many(["file1.txt", "file2.txt"])

        # Removed files should not exist
        assert not vfs.exists("file1.txt")
        assert not vfs.exists("file2.txt")
        # Remaining file should still exist
        assert vfs.exists("file3.txt")

    def test_remove_many_missing_file_removes_preceding(self):
        """Test that remove_many removes files up to the missing one."""
        vfs = VirtualFS({})

        vfs.write("file1.txt", b"content 1")

        with pytest.raises(FileNotFoundError):
            vfs.remove_many(["file1.txt", "file2.txt"])

        # file1.txt was removed before file2.txt failed
        assert not vfs.exists("file1.txt")

    def test_remove_many_empty_list(self):
        """Test that removing empty list works."""
        vfs = VirtualFS({})

        # Should not raise
        vfs.remove_many([])
