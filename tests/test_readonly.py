"""Tests for ReadOnlyFS."""

import os

import pytest

from monkeyfs import VirtualFS, patch
from monkeyfs.readonly import ReadOnlyFS


def _make_ro():
    """Create a ReadOnlyFS wrapping a VirtualFS with some files."""
    vfs = VirtualFS({})
    vfs.write("file.txt", b"hello")
    vfs.write("data/report.csv", b"a,b,c")
    vfs.mkdir("empty_dir")
    return ReadOnlyFS(vfs)


class TestReadOnlyFSReads:
    """Read operations should delegate transparently."""

    def test_read_file(self):
        ro = _make_ro()
        assert ro.read("file.txt") == b"hello"

    def test_open_read_text(self):
        ro = _make_ro()
        f = ro.open("file.txt", "r")
        assert f.read() == "hello"

    def test_open_read_binary(self):
        ro = _make_ro()
        f = ro.open("file.txt", "rb")
        assert f.read() == b"hello"

    def test_exists(self):
        ro = _make_ro()
        assert ro.exists("file.txt")
        assert not ro.exists("nope.txt")

    def test_isfile(self):
        ro = _make_ro()
        assert ro.isfile("file.txt")
        assert not ro.isfile("data")

    def test_isdir(self):
        ro = _make_ro()
        assert ro.isdir("data")
        assert not ro.isdir("file.txt")

    def test_list(self):
        ro = _make_ro()
        entries = ro.list("/")
        assert "file.txt" in entries
        assert "data" in entries

    def test_list_recursive(self):
        ro = _make_ro()
        entries = ro.list("/", recursive=True)
        assert "data/report.csv" in entries

    def test_stat(self):
        ro = _make_ro()
        meta = ro.stat("file.txt")
        assert meta.size == 5
        assert not meta.is_dir

    def test_getcwd(self):
        ro = _make_ro()
        assert ro.getcwd() == "/"

    def test_chdir(self):
        ro = _make_ro()
        ro.chdir("data")
        assert ro.getcwd() == "/data"

    def test_glob(self):
        ro = _make_ro()
        matches = ro.glob("*.txt")
        assert "file.txt" in matches


class TestReadOnlyFSBlocks:
    """Write operations should raise PermissionError."""

    def test_open_write(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("file.txt", "w")

    def test_open_append(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("file.txt", "a")

    def test_open_exclusive(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("new.txt", "x")

    def test_open_readwrite(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("file.txt", "r+")

    def test_write(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.write("file.txt", b"new")

    def test_write_many(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.write_many({"a.txt": b"a"})

    def test_remove(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.remove("file.txt")

    def test_remove_many(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.remove_many(["file.txt"])

    def test_mkdir(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.mkdir("newdir")

    def test_makedirs(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.makedirs("a/b/c")

    def test_rename(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.rename("file.txt", "other.txt")

    def test_rmdir(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.rmdir("empty_dir")

    def test_replace(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.replace("file.txt", "other.txt")

    def test_truncate(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.truncate("file.txt", 0)

    def test_symlink(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.symlink("file.txt", "link.txt")

    def test_link(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.link("file.txt", "link.txt")

    def test_chmod(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.chmod("file.txt", 0o644)

    def test_chown(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.chown("file.txt", 0, 0)


class TestReadOnlyFSAccess:
    """access() should deny write, allow read."""

    def test_write_denied(self):
        ro = _make_ro()
        assert not ro.access("file.txt", os.W_OK)

    def test_read_allowed(self):
        ro = _make_ro()
        assert ro.access("file.txt", os.R_OK)

    def test_exists_check(self):
        ro = _make_ro()
        assert ro.access("file.txt", os.F_OK)


class TestReadOnlyFSWithPatch:
    """ReadOnlyFS should work through the patch() context."""

    def test_patched_open_read(self):
        ro = _make_ro()
        with patch(ro):
            with open("file.txt", "r") as f:
                assert f.read() == "hello"

    def test_patched_open_write_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                open("file.txt", "w")

    def test_patched_os_remove_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                os.remove("file.txt")

    def test_patched_os_mkdir_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                os.mkdir("newdir")

    def test_patched_os_rename_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                os.rename("file.txt", "other.txt")


class TestReadOnlyFSComposition:
    """ReadOnlyFS should compose with different filesystem types."""

    def test_wraps_virtualfs(self):
        vfs = VirtualFS({})
        vfs.write("test.txt", b"data")
        ro = ReadOnlyFS(vfs)
        assert ro.read("test.txt") == b"data"
        with pytest.raises(PermissionError):
            ro.write("test.txt", b"new")

    def test_inner_fs_still_writable(self):
        """Wrapping doesn't affect the inner filesystem."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"data")
        ro = ReadOnlyFS(vfs)
        # Inner FS is still writable
        vfs.write("test.txt", b"updated")
        # ReadOnlyFS sees the update
        assert ro.read("test.txt") == b"updated"
