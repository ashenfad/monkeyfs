"""Tests for VirtualFS optional methods and POSIX error paths."""

import errno
import os

import pytest

from monkeyfs import VirtualFS


# ---------------------------------------------------------------------------
# chmod
# ---------------------------------------------------------------------------


class TestVFSChmod:
    def test_chmod_file_noop(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")
        vfs.chmod("file.txt", 0o755)
        assert vfs.read("file.txt") == b"hello"

    def test_chmod_dir_noop(self):
        vfs = VirtualFS({})
        vfs.mkdir("mydir")
        vfs.chmod("mydir", 0o700)  # should not raise

    def test_chmod_nonexistent_raises(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.chmod("nope.txt", 0o644)


# ---------------------------------------------------------------------------
# chown
# ---------------------------------------------------------------------------


class TestVFSChown:
    def test_chown_file_noop(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"data")
        vfs.chown("file.txt", 1000, 1000)  # should not raise

    def test_chown_nonexistent_raises(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.chown("missing.txt", 1000, 1000)


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------


class TestVFSAccess:
    def test_access_existing_file(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"x")
        assert vfs.access("file.txt", os.R_OK) is True

    def test_access_existing_dir(self):
        vfs = VirtualFS({})
        vfs.mkdir("d")
        assert vfs.access("d", os.R_OK) is True

    def test_access_nonexistent(self):
        vfs = VirtualFS({})
        assert vfs.access("nowhere", os.F_OK) is False


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------


class TestVFSLink:
    def test_link_copies_content(self):
        vfs = VirtualFS({})
        vfs.write("src.txt", b"payload")
        vfs.link("src.txt", "dst.txt")
        assert vfs.read("src.txt") == b"payload"
        assert vfs.read("dst.txt") == b"payload"

    def test_link_nonexistent_raises(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.link("ghost.txt", "dst.txt")


# ---------------------------------------------------------------------------
# truncate
# ---------------------------------------------------------------------------


class TestVFSTruncate:
    def test_truncate_shortens(self):
        vfs = VirtualFS({})
        vfs.write("data.bin", b"0123456789")
        vfs.truncate("data.bin", 5)
        assert vfs.read("data.bin") == b"01234"

    def test_truncate_to_zero(self):
        vfs = VirtualFS({})
        vfs.write("data.bin", b"stuff")
        vfs.truncate("data.bin", 0)
        assert vfs.read("data.bin") == b""

    def test_truncate_nonexistent_raises(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.truncate("nope.bin", 5)


# ---------------------------------------------------------------------------
# replace
# ---------------------------------------------------------------------------


class TestVFSReplace:
    def test_replace_moves_file(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"alpha")
        vfs.replace("a.txt", "b.txt")
        assert vfs.read("b.txt") == b"alpha"
        assert not vfs.exists("a.txt")

    def test_replace_overwrites_dst(self):
        vfs = VirtualFS({})
        vfs.write("src.txt", b"new")
        vfs.write("dst.txt", b"old")
        vfs.replace("src.txt", "dst.txt")
        assert vfs.read("dst.txt") == b"new"
        assert not vfs.exists("src.txt")

    def test_replace_nonexistent_raises(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.replace("missing.txt", "dst.txt")


# ---------------------------------------------------------------------------
# readlink / symlink
# ---------------------------------------------------------------------------


class TestVFSReadlinkAndSymlink:
    def test_readlink_raises(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"x")
        with pytest.raises(OSError) as exc_info:
            vfs.readlink("file.txt")
        assert exc_info.value.errno == errno.EINVAL

    def test_symlink_raises(self):
        vfs = VirtualFS({})
        with pytest.raises(OSError) as exc_info:
            vfs.symlink("target", "link")
        assert exc_info.value.errno == errno.EPERM


# ---------------------------------------------------------------------------
# islink / lexists
# ---------------------------------------------------------------------------


class TestVFSIslinkAndLexists:
    def test_islink_file_false(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"x")
        assert vfs.islink("file.txt") is False

    def test_islink_dir_false(self):
        vfs = VirtualFS({})
        vfs.mkdir("d")
        assert vfs.islink("d") is False

    def test_islink_nonexistent_false(self):
        vfs = VirtualFS({})
        assert vfs.islink("nope") is False

    def test_lexists_file_true(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"x")
        assert vfs.lexists("file.txt") is True

    def test_lexists_nonexistent_false(self):
        vfs = VirtualFS({})
        assert vfs.lexists("nowhere") is False


# ---------------------------------------------------------------------------
# samefile
# ---------------------------------------------------------------------------


class TestVFSSamefile:
    def test_samefile_same_path(self):
        vfs = VirtualFS({})
        vfs.write("f.txt", b"x")
        assert vfs.samefile("f.txt", "f.txt") is True

    def test_samefile_different_normalization(self):
        vfs = VirtualFS({})
        vfs.write("f.txt", b"x")
        assert vfs.samefile("./f.txt", "f.txt") is True

    def test_samefile_different_files(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"x")
        vfs.write("b.txt", b"x")
        assert vfs.samefile("a.txt", "b.txt") is False

    def test_samefile_nonexistent(self):
        vfs = VirtualFS({})
        assert vfs.samefile("x.txt", "y.txt") is False

    def test_samefile_cwd_relative(self):
        vfs = VirtualFS({})
        vfs.write("sub/f.txt", b"x")
        vfs.chdir("sub")
        assert vfs.samefile("f.txt", "/sub/f.txt") is True
        assert vfs.samefile("f.txt", "f.txt") is True


# ---------------------------------------------------------------------------
# realpath
# ---------------------------------------------------------------------------


class TestVFSRealpath:
    def test_realpath_relative(self):
        vfs = VirtualFS({})
        assert vfs.realpath("file.txt") == "/file.txt"

    def test_realpath_dotdot(self):
        vfs = VirtualFS({})
        assert vfs.realpath("dir/../file.txt") == "/file.txt"

    def test_realpath_absolute(self):
        vfs = VirtualFS({})
        assert vfs.realpath("/file.txt") == "/file.txt"

    def test_realpath_cwd_relative(self):
        vfs = VirtualFS({})
        vfs.makedirs("sub")
        vfs.chdir("sub")
        assert vfs.realpath("file.txt") == "/sub/file.txt"


# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


class TestVFSGlob:
    def test_glob_star(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"a")
        vfs.write("b.py", b"b")
        assert vfs.glob("*.txt") == ["a.txt"]

    def test_glob_nested(self):
        vfs = VirtualFS({})
        vfs.write("dir/a.py", b"code")
        result = vfs.glob("dir/*.py")
        assert result == ["dir/a.py"]

    def test_glob_absolute(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"a")
        assert vfs.glob("/*.txt") == ["/a.txt"]

    def test_glob_no_match(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"a")
        assert vfs.glob("*.xyz") == []

    def test_glob_with_cwd(self):
        vfs = VirtualFS({})
        vfs.write("sub/code.py", b"x")
        vfs.chdir("sub")
        assert vfs.glob("*.py") == ["code.py"]


# ---------------------------------------------------------------------------
# get_metadata_snapshot
# ---------------------------------------------------------------------------


class TestVFSGetMetadataSnapshot:
    def test_returns_copy(self):
        vfs = VirtualFS({})
        vfs.write("f.txt", b"data")
        snap = vfs.get_metadata_snapshot()
        snap["f.txt"] = None  # mutate the copy
        # original metadata should be unaffected
        real = vfs.get_metadata_snapshot()
        assert real["f.txt"] is not None

    def test_reflects_state(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"abc")
        vfs.write("b.txt", b"xy")
        snap = vfs.get_metadata_snapshot()
        assert "a.txt" in snap
        assert snap["a.txt"].size == 3
        assert "b.txt" in snap
        assert snap["b.txt"].size == 2

    def test_empty_vfs(self):
        vfs = VirtualFS({})
        assert vfs.get_metadata_snapshot() == {}


# ---------------------------------------------------------------------------
# POSIX error paths
# ---------------------------------------------------------------------------


class TestVFSPosixErrors:
    def test_mkdir_no_parents_missing_parent(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.mkdir("a/b/c", parents=False)

    def test_open_write_missing_parent(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.open("nonexistent_dir/file.txt", "w")

    def test_open_exclusive_existing(self):
        vfs = VirtualFS({})
        vfs.write("exists.txt", b"hi")
        with pytest.raises(FileExistsError):
            vfs.open("exists.txt", "x")

    def test_rename_nonexistent(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.rename("ghost.txt", "dst.txt")

    def test_chdir_nonexistent(self):
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.chdir("no_such_dir")

    def test_chdir_to_file(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"data")
        with pytest.raises(FileNotFoundError):
            vfs.chdir("file.txt")

    def test_write_requires_bytes(self):
        vfs = VirtualFS({})
        with pytest.raises(TypeError):
            vfs.write("file.txt", "not_bytes")
