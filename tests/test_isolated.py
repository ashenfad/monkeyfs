"""Tests for IsolatedFS direct API."""

import os

import pytest

from monkeyfs import IsolatedFS
from monkeyfs.base import FileInfo, FileMetadata


# ---------------------------------------------------------------------------
# Core read/write
# ---------------------------------------------------------------------------


class TestIsolatedCoreReadWrite:
    """Test basic read/write operations on IsolatedFS."""

    def test_write_and_read(self, tmp_path):
        """Test writing bytes and reading them back."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("hello.txt", b"hello world")
        assert fs.read("hello.txt") == b"hello world"

    def test_write_creates_parents(self, tmp_path):
        """Test that write auto-creates parent directories."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("a/b/c.txt", b"deep")
        assert fs.read("a/b/c.txt") == b"deep"
        assert fs.isdir("a") is True
        assert fs.isdir("a/b") is True

    def test_write_append(self, tmp_path):
        """Test that write with mode='a' appends content."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("log.txt", b"line1\n")
        fs.write("log.txt", b"line2\n", mode="a")
        assert fs.read("log.txt") == b"line1\nline2\n"

    def test_read_nonexistent_raises(self, tmp_path):
        """Test that reading a missing file raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.read("missing.txt")

    def test_write_many(self, tmp_path):
        """Test writing multiple files at once."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write_many({
            "a.txt": b"alpha",
            "b.txt": b"bravo",
            "c.txt": b"charlie",
        })
        assert fs.read("a.txt") == b"alpha"
        assert fs.read("b.txt") == b"bravo"
        assert fs.read("c.txt") == b"charlie"

    def test_remove(self, tmp_path):
        """Test removing a file."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("temp.txt", b"gone soon")
        assert fs.exists("temp.txt") is True
        fs.remove("temp.txt")
        assert fs.exists("temp.txt") is False

    def test_remove_nonexistent_raises(self, tmp_path):
        """Test that removing a missing file raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.remove("nope.txt")

    def test_remove_directory_raises(self, tmp_path):
        """Test that removing a directory raises IsADirectoryError."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("mydir")
        with pytest.raises(IsADirectoryError):
            fs.remove("mydir")

    def test_remove_many(self, tmp_path):
        """Test removing multiple files, leaving others intact."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write_many({
            "a.txt": b"a",
            "b.txt": b"b",
            "c.txt": b"c",
        })
        fs.remove_many(["a.txt", "b.txt"])
        assert fs.exists("a.txt") is False
        assert fs.exists("b.txt") is False
        assert fs.exists("c.txt") is True


# ---------------------------------------------------------------------------
# open()
# ---------------------------------------------------------------------------


class TestIsolatedOpen:
    """Test IsolatedFS.open() method."""

    def test_open_read_text(self, tmp_path):
        """Test opening a file for text reading."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"hello text")
        with fs.open("file.txt", "r") as f:
            content = f.read()
        assert content == "hello text"
        assert isinstance(content, str)

    def test_open_read_binary(self, tmp_path):
        """Test opening a file for binary reading."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.bin", b"\x00\x01\x02")
        with fs.open("file.bin", "rb") as f:
            content = f.read()
        assert content == b"\x00\x01\x02"
        assert isinstance(content, bytes)

    def test_open_write_text(self, tmp_path):
        """Test writing text via open('w')."""
        fs = IsolatedFS(str(tmp_path), {})
        with fs.open("file.txt", "w") as f:
            f.write("written via open")
        assert fs.read("file.txt") == b"written via open"

    def test_open_write_binary(self, tmp_path):
        """Test writing bytes via open('wb')."""
        fs = IsolatedFS(str(tmp_path), {})
        with fs.open("file.bin", "wb") as f:
            f.write(b"\xde\xad")
        assert fs.read("file.bin") == b"\xde\xad"

    def test_open_append(self, tmp_path):
        """Test appending via open('a')."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"first ")
        with fs.open("file.txt", "a") as f:
            f.write("second")
        assert fs.read("file.txt") == b"first second"

    def test_open_nonexistent_raises(self, tmp_path):
        """Test that opening a missing file for read raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.open("missing.txt", "r")

    def test_open_context_manager(self, tmp_path):
        """Test that open works as a context manager and auto-closes."""
        fs = IsolatedFS(str(tmp_path), {})
        with fs.open("ctx.txt", "w") as f:
            f.write("context")
        # File should be closed and content persisted
        assert fs.read("ctx.txt") == b"context"


# ---------------------------------------------------------------------------
# Existence and type
# ---------------------------------------------------------------------------


class TestIsolatedExistsAndType:
    """Test exists/isfile/isdir."""

    def test_exists_file(self, tmp_path):
        """Test that exists returns True for files."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"x")
        assert fs.exists("file.txt") is True

    def test_exists_dir(self, tmp_path):
        """Test that exists returns True for directories."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("mydir")
        assert fs.exists("mydir") is True

    def test_exists_nonexistent(self, tmp_path):
        """Test that exists returns False for missing paths."""
        fs = IsolatedFS(str(tmp_path), {})
        assert fs.exists("nope") is False

    def test_isfile(self, tmp_path):
        """Test isfile: True for files, False for directories."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"x")
        fs.mkdir("adir")
        assert fs.isfile("file.txt") is True
        assert fs.isfile("adir") is False

    def test_isdir(self, tmp_path):
        """Test isdir: True for dirs, False for files."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("adir")
        fs.write("file.txt", b"x")
        assert fs.isdir("adir") is True
        assert fs.isdir("file.txt") is False

    def test_isdir_root(self, tmp_path):
        """Test that root directory is always a directory."""
        fs = IsolatedFS(str(tmp_path), {})
        assert fs.isdir("/") is True


# ---------------------------------------------------------------------------
# Directory operations
# ---------------------------------------------------------------------------


class TestIsolatedDirectoryOps:
    """Test mkdir, makedirs, rmdir."""

    def test_mkdir(self, tmp_path):
        """Test creating a single directory."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("newdir")
        assert fs.isdir("newdir") is True

    def test_mkdir_parents(self, tmp_path):
        """Test mkdir with parents=True creates the full tree."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("a/b/c", parents=True)
        assert fs.isdir("a") is True
        assert fs.isdir("a/b") is True
        assert fs.isdir("a/b/c") is True

    def test_mkdir_no_parents_raises(self, tmp_path):
        """Test mkdir with parents=False raises if parent missing."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.mkdir("x/y/z", parents=False)

    def test_mkdir_exist_ok_false_raises(self, tmp_path):
        """Test mkdir with exist_ok=False raises if directory exists."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("dup")
        with pytest.raises(FileExistsError):
            fs.mkdir("dup", exist_ok=False)

    def test_makedirs(self, tmp_path):
        """Test makedirs creates entire directory tree."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.makedirs("a/b/c")
        assert fs.isdir("a") is True
        assert fs.isdir("a/b") is True
        assert fs.isdir("a/b/c") is True

    def test_rmdir(self, tmp_path):
        """Test removing an empty directory."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("empty")
        fs.rmdir("empty")
        assert fs.exists("empty") is False

    def test_rmdir_nonempty_raises(self, tmp_path):
        """Test that rmdir on a non-empty directory raises OSError."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("parent")
        fs.write("parent/child.txt", b"x")
        with pytest.raises(OSError):
            fs.rmdir("parent")

    def test_rmdir_nonexistent_raises(self, tmp_path):
        """Test that rmdir on a nonexistent path raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.rmdir("ghost")


# ---------------------------------------------------------------------------
# Rename / replace
# ---------------------------------------------------------------------------


class TestIsolatedRename:
    """Test rename and replace."""

    def test_rename_file(self, tmp_path):
        """Test renaming a file moves content to new name."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("old.txt", b"payload")
        fs.rename("old.txt", "new.txt")
        assert fs.exists("old.txt") is False
        assert fs.read("new.txt") == b"payload"

    def test_rename_directory(self, tmp_path):
        """Test renaming a directory."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("src")
        fs.write("src/file.txt", b"inside")
        fs.rename("src", "dst")
        assert fs.exists("src") is False
        assert fs.isdir("dst") is True
        assert fs.read("dst/file.txt") == b"inside"

    def test_rename_nonexistent_raises(self, tmp_path):
        """Test renaming a missing path raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.rename("nope", "also_nope")

    def test_replace_alias(self, tmp_path):
        """Test that replace() works the same as rename()."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("orig.txt", b"data")
        fs.replace("orig.txt", "moved.txt")
        assert fs.exists("orig.txt") is False
        assert fs.read("moved.txt") == b"data"


# ---------------------------------------------------------------------------
# listdir / list
# ---------------------------------------------------------------------------


class TestIsolatedListdir:
    """Test listdir and list."""

    def test_listdir_files(self, tmp_path):
        """Test listing files in root."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("a.txt", b"a")
        fs.write("b.txt", b"b")
        assert sorted(fs.listdir("/")) == ["a.txt", "b.txt"]

    def test_listdir_subdir(self, tmp_path):
        """Test listing files in a subdirectory."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("sub/x.txt", b"x")
        fs.write("sub/y.txt", b"y")
        assert sorted(fs.listdir("sub")) == ["x.txt", "y.txt"]

    def test_listdir_empty(self, tmp_path):
        """Test listing an empty directory returns empty list."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("empty")
        assert fs.listdir("empty") == []

    def test_listdir_recursive(self, tmp_path):
        """Test recursive listing."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("a.txt", b"a")
        fs.write("sub/b.txt", b"b")
        result = fs.listdir("/", recursive=True)
        assert "a.txt" in result
        assert "sub" in result
        assert os.path.join("sub", "b.txt") in result

    def test_list_alias(self, tmp_path):
        """Test that list() returns the same as listdir()."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"x")
        assert fs.list("/") == fs.listdir("/")


# ---------------------------------------------------------------------------
# stat / getsize
# ---------------------------------------------------------------------------


class TestIsolatedStat:
    """Test stat and getsize."""

    def test_stat_file(self, tmp_path):
        """Test stat returns FileMetadata with correct size."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("data.bin", b"12345")
        meta = fs.stat("data.bin")
        assert isinstance(meta, FileMetadata)
        assert meta.size == 5

    def test_stat_nonexistent_raises(self, tmp_path):
        """Test stat on missing path raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.stat("ghost.txt")

    def test_getsize(self, tmp_path):
        """Test getsize returns correct byte count."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("sized.txt", b"abcdefghij")
        assert fs.getsize("sized.txt") == 10


# ---------------------------------------------------------------------------
# CWD and paths
# ---------------------------------------------------------------------------


class TestIsolatedCwdAndPaths:
    """Test getcwd, chdir, resolve_path."""

    def test_getcwd_default(self, tmp_path):
        """Test default working directory is /."""
        fs = IsolatedFS(str(tmp_path), {})
        assert fs.getcwd() == "/"

    def test_chdir_and_getcwd(self, tmp_path):
        """Test chdir changes cwd and getcwd reflects it."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("work")
        fs.chdir("work")
        assert fs.getcwd() == "/work"

    def test_chdir_nonexistent_raises(self, tmp_path):
        """Test chdir to missing directory raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(FileNotFoundError):
            fs.chdir("nowhere")

    def test_resolve_path_relative(self, tmp_path):
        """Test that relative paths resolve against cwd."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("sub")
        fs.chdir("sub")
        resolved = fs.resolve_path("file.txt")
        assert resolved == "/sub/file.txt"

    def test_resolve_path_absolute(self, tmp_path):
        """Test that absolute paths stay absolute."""
        fs = IsolatedFS(str(tmp_path), {})
        resolved = fs.resolve_path("/top/level.txt")
        assert resolved == "/top/level.txt"


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


class TestIsolatedGlob:
    """Test glob pattern matching."""

    def test_glob_star(self, tmp_path):
        """Test *.txt matches only .txt files."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("a.txt", b"a")
        fs.write("b.txt", b"b")
        fs.write("c.py", b"c")
        result = sorted(fs.glob("*.txt"))
        assert result == ["a.txt", "b.txt"]

    def test_glob_recursive(self, tmp_path):
        """Test **/*.py matches nested .py files."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("top.py", b"t")
        fs.write("pkg/mod.py", b"m")
        fs.write("pkg/sub/deep.py", b"d")
        result = sorted(fs.glob("**/*.py"))
        assert "pkg/mod.py" in result
        assert "pkg/sub/deep.py" in result

    def test_glob_absolute(self, tmp_path):
        """Test absolute glob pattern /dir/*.txt."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("dir/a.txt", b"a")
        fs.write("dir/b.txt", b"b")
        fs.write("dir/c.py", b"c")
        result = sorted(fs.glob("/dir/*.txt"))
        assert result == ["/dir/a.txt", "/dir/b.txt"]

    def test_glob_no_matches(self, tmp_path):
        """Test glob with no matches returns empty list."""
        fs = IsolatedFS(str(tmp_path), {})
        assert fs.glob("*.xyz") == []


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestIsolatedPathValidation:
    """Test that paths escaping root are rejected."""

    def test_path_escape_raises(self, tmp_path):
        """Test that ../secret raises PermissionError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(PermissionError):
            fs._validate_path("../secret")

    def test_dotdot_escape_raises(self, tmp_path):
        """Test that sub/../../secret raises PermissionError."""
        fs = IsolatedFS(str(tmp_path), {})
        with pytest.raises(PermissionError):
            fs._validate_path("sub/../../secret")


# ---------------------------------------------------------------------------
# list_detailed / listdir_detailed
# ---------------------------------------------------------------------------


class TestIsolatedListDetailed:
    """Test list_detailed and listdir_detailed."""

    def test_list_detailed(self, tmp_path):
        """Test list_detailed returns FileInfo objects."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("info.txt", b"info")
        result = fs.list_detailed("/")
        assert len(result) == 1
        item = result[0]
        assert isinstance(item, FileInfo)
        assert item.name == "info.txt"
        assert item.is_dir is False
        assert item.size == 4

    def test_list_detailed_recursive(self, tmp_path):
        """Test list_detailed with recursive=True."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("a.txt", b"a")
        fs.write("sub/b.txt", b"bb")
        result = fs.list_detailed("/", recursive=True)
        names = [fi.name for fi in result]
        assert "a.txt" in names
        assert "b.txt" in names
        assert "sub" in names


# ---------------------------------------------------------------------------
# Optional / OS-level methods
# ---------------------------------------------------------------------------


class TestIsolatedOptionalMethods:
    """Test optional methods that delegate to real OS calls."""

    def test_chmod(self, tmp_path):
        """Test chmod changes file permissions."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("script.sh", b"#!/bin/sh")
        fs.chmod("script.sh", 0o755)
        real_path = tmp_path / "script.sh"
        mode = os.stat(real_path).st_mode & 0o777
        assert mode == 0o755

    def test_access_readable(self, tmp_path):
        """Test access returns True for a readable file."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("read.txt", b"ok")
        assert fs.access("read.txt", os.R_OK) is True

    def test_access_nonexistent(self, tmp_path):
        """Test access returns False for missing file."""
        fs = IsolatedFS(str(tmp_path), {})
        assert fs.access("nope.txt", os.R_OK) is False

    def test_link_creates_hardlink(self, tmp_path):
        """Test link creates a hard link with the same inode."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("source.txt", b"shared")
        fs.link("source.txt", "linked.txt")
        assert fs.read("linked.txt") == b"shared"
        src_ino = os.stat(tmp_path / "source.txt").st_ino
        dst_ino = os.stat(tmp_path / "linked.txt").st_ino
        assert src_ino == dst_ino

    def test_truncate(self, tmp_path):
        """Test truncate shortens file to given length."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("data.txt", b"0123456789")
        fs.truncate("data.txt", 5)
        assert fs.getsize("data.txt") == 5
        assert fs.read("data.txt") == b"01234"

    def test_truncate_to_zero(self, tmp_path):
        """Test truncate to zero empties the file."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("data.txt", b"content")
        fs.truncate("data.txt", 0)
        assert fs.getsize("data.txt") == 0
        assert fs.read("data.txt") == b""

    def test_symlink_and_readlink(self, tmp_path):
        """Test symlink creation and readlink."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("target.txt", b"target data")
        fs.symlink("target.txt", "link.txt")
        assert fs.islink("link.txt")
        target_str = fs.readlink("link.txt")
        assert "target.txt" in target_str

    def test_symlink_and_islink(self, tmp_path):
        """Test islink returns True for symbolic links."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("real.txt", b"data")
        fs.symlink("real.txt", "sym.txt")
        assert fs.islink("sym.txt") is True

    def test_islink_regular_file(self, tmp_path):
        """Test islink returns False for regular files."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("plain.txt", b"x")
        assert fs.islink("plain.txt") is False

    def test_islink_cwd_relative(self, tmp_path):
        """Test islink resolves relative paths against CWD."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.mkdir("subdir")
        fs.write("subdir/target.txt", b"data")
        fs.symlink("subdir/target.txt", "subdir/link.txt")
        fs.chdir("subdir")
        assert fs.islink("link.txt") is True
        assert fs.islink("target.txt") is False

    def test_lexists(self, tmp_path):
        """Test lexists: True for existing file, False for missing."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("here.txt", b"x")
        assert fs.lexists("here.txt") is True
        assert fs.lexists("not_here.txt") is False

    def test_samefile_same(self, tmp_path):
        """Test samefile returns True for the same path."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"x")
        assert fs.samefile("file.txt", "file.txt") is True

    def test_samefile_different(self, tmp_path):
        """Test samefile returns False for different files."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("one.txt", b"1")
        fs.write("two.txt", b"2")
        assert fs.samefile("one.txt", "two.txt") is False

    def test_realpath(self, tmp_path):
        """Test realpath returns canonical virtual path."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("file.txt", b"x")
        rp = fs.realpath("file.txt")
        assert rp == "/file.txt"

    def test_get_metadata_snapshot(self, tmp_path):
        """Test get_metadata_snapshot returns tracked entries."""
        fs = IsolatedFS(str(tmp_path), {})
        fs.write("a.txt", b"aaa")
        fs.write("b.txt", b"bb")
        snapshot = fs.get_metadata_snapshot()
        assert len(snapshot) == 2
        assert all(isinstance(v, FileMetadata) for v in snapshot.values())
