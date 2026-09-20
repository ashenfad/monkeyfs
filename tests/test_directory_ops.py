"""Tests for VirtualFS directory operations (mkdir, rmdir) and CWD (chdir, getcwd).

These tests verify the explicit directory support and current working directory
functionality added to VirtualFS.
"""

import errno
import os

import pytest

from monkeyfs import VirtualFS, patch


class TestVFSMkdir:
    """Test VirtualFS.mkdir() functionality."""

    def test_mkdir_creates_directory(self):
        """mkdir creates an explicit directory entry."""
        vfs = VirtualFS({})

        vfs.mkdir("mydir")

        assert vfs.isdir("mydir") is True
        assert vfs.exists("mydir") is True
        assert vfs.isfile("mydir") is False

    def test_mkdir_with_leading_slash(self):
        """mkdir works with absolute paths."""
        vfs = VirtualFS({})

        vfs.mkdir("/mydir")

        assert vfs.isdir("mydir") is True
        assert vfs.isdir("/mydir") is True

    def test_mkdir_exist_ok_true(self):
        """mkdir with exist_ok=True doesn't raise on existing dir."""
        vfs = VirtualFS({})

        vfs.mkdir("mydir")
        vfs.mkdir("mydir", exist_ok=True)  # Should not raise

        assert vfs.isdir("mydir") is True

    def test_mkdir_exist_ok_false_raises(self):
        """mkdir with exist_ok=False raises on existing dir."""
        vfs = VirtualFS({})

        vfs.mkdir("mydir")

        with pytest.raises(FileExistsError):
            vfs.mkdir("mydir", exist_ok=False)

    def test_mkdir_raises_on_file(self):
        """mkdir raises if path is an existing file."""
        vfs = VirtualFS({})

        vfs.write("myfile", b"content")

        with pytest.raises(FileExistsError):
            vfs.mkdir("myfile")

    def test_mkdir_empty_dir_has_no_children(self):
        """Empty directory created by mkdir has no children."""
        vfs = VirtualFS({})

        vfs.mkdir("empty_dir")

        children = vfs.list("empty_dir")
        assert children == []


class TestVFSMakedirs:
    """Test VirtualFS.makedirs() functionality."""

    def test_makedirs_creates_tree(self):
        """makedirs creates entire directory tree."""
        vfs = VirtualFS({})

        vfs.makedirs("a/b/c")

        assert vfs.isdir("a") is True
        assert vfs.isdir("a/b") is True
        assert vfs.isdir("a/b/c") is True

    def test_makedirs_partial_tree_exists(self):
        """makedirs works when partial tree already exists."""
        vfs = VirtualFS({})

        vfs.mkdir("a")
        vfs.makedirs("a/b/c")

        assert vfs.isdir("a") is True
        assert vfs.isdir("a/b") is True
        assert vfs.isdir("a/b/c") is True

    def test_makedirs_raises_on_file_in_path(self):
        """makedirs raises if any path component is a file."""
        vfs = VirtualFS({})

        vfs.write("a", b"content")

        with pytest.raises(FileExistsError):
            vfs.makedirs("a/b/c")

    def test_makedirs_exist_ok_false_raises_on_an_existing_directory(self):
        """An existing directory is an error unless exist_ok says otherwise."""
        vfs = VirtualFS({})
        vfs.mkdir("d")

        with pytest.raises(FileExistsError) as caught:
            vfs.makedirs("d", exist_ok=False)

        assert caught.value.errno == errno.EEXIST

    def test_makedirs_exist_ok_false_raises_on_an_implicit_directory(self):
        """A directory a file path passes through exists like any other."""
        vfs = VirtualFS({})
        vfs.write("d/file.txt", b"content")

        assert vfs.isdir("d") is True
        with pytest.raises(FileExistsError) as caught:
            vfs.makedirs("d", exist_ok=False)

        assert caught.value.errno == errno.EEXIST

    def test_makedirs_exist_ok_true_is_silent(self):
        """The default keeps working on a tree that is already there."""
        vfs = VirtualFS({})
        vfs.makedirs("a/b/c")

        vfs.makedirs("a/b/c")
        vfs.makedirs("a/b/c", exist_ok=True)

        assert vfs.isdir("a/b/c") is True

    def test_makedirs_exist_ok_false_still_creates_a_missing_tree(self):
        """Raising on what exists must not stop it creating what does not."""
        vfs = VirtualFS({})
        vfs.mkdir("a")

        vfs.makedirs("a/b/c", exist_ok=False)

        assert vfs.isdir("a/b") is True
        assert vfs.isdir("a/b/c") is True

    def test_mkdir_parents_exist_ok_false_raises_on_an_existing_directory(self):
        """parents=True must not soften the check exist_ok=False asks for."""
        vfs = VirtualFS({})
        vfs.makedirs("a/b")

        with pytest.raises(FileExistsError) as caught:
            vfs.mkdir("a/b", parents=True, exist_ok=False)

        assert caught.value.errno == errno.EEXIST

    def test_mkdir_parents_exist_ok_true_is_silent(self):
        vfs = VirtualFS({})
        vfs.makedirs("a/b")

        vfs.mkdir("a/b", parents=True, exist_ok=True)

        assert vfs.isdir("a/b") is True


class TestVFSRmdir:
    """Test VirtualFS.rmdir() functionality."""

    def test_rmdir_removes_empty_directory(self):
        """rmdir removes an empty directory."""
        vfs = VirtualFS({})

        vfs.mkdir("mydir")
        assert vfs.isdir("mydir") is True

        vfs.rmdir("mydir")

        assert vfs.exists("mydir") is False
        assert vfs.isdir("mydir") is False

    def test_rmdir_raises_on_non_empty_directory(self):
        """rmdir raises on non-empty directory."""
        vfs = VirtualFS({})

        vfs.write("mydir/file.txt", b"content")

        with pytest.raises(OSError, match="not empty"):
            vfs.rmdir("mydir")

    def test_rmdir_raises_on_file(self):
        """rmdir raises on file path."""
        vfs = VirtualFS({})

        vfs.write("myfile", b"content")

        with pytest.raises(NotADirectoryError):
            vfs.rmdir("myfile")

    def test_rmdir_raises_on_nonexistent(self):
        """rmdir raises on nonexistent path."""
        vfs = VirtualFS({})

        with pytest.raises(FileNotFoundError):
            vfs.rmdir("nonexistent")

    def test_rmdir_with_nested_empty_dirs(self):
        """rmdir only removes immediate directory, not parents."""
        vfs = VirtualFS({})

        vfs.makedirs("a/b/c")

        vfs.rmdir("a/b/c")

        assert vfs.isdir("a/b") is True
        assert vfs.isdir("a") is True
        assert vfs.exists("a/b/c") is False


class TestVFSChdir:
    """Test VirtualFS.chdir() and getcwd() functionality."""

    def test_getcwd_default_root(self):
        """getcwd returns / by default."""
        vfs = VirtualFS({})

        assert vfs.getcwd() == "/"

    def test_chdir_updates_cwd(self):
        """chdir updates the current working directory."""
        vfs = VirtualFS({})

        vfs.mkdir("mydir")
        vfs.chdir("mydir")

        assert vfs.getcwd() == "/mydir"

    def test_chdir_absolute_path(self):
        """chdir works with absolute paths."""
        vfs = VirtualFS({})

        vfs.makedirs("a/b/c")
        vfs.chdir("/a/b/c")

        assert vfs.getcwd() == "/a/b/c"

    def test_chdir_relative_path(self):
        """chdir works with relative paths."""
        vfs = VirtualFS({})

        vfs.makedirs("a/b/c")
        vfs.chdir("a")
        vfs.chdir("b")
        vfs.chdir("c")

        assert vfs.getcwd() == "/a/b/c"

    def test_chdir_parent_directory(self):
        """chdir works with .. to go up."""
        vfs = VirtualFS({})

        vfs.makedirs("a/b")
        vfs.chdir("a/b")
        vfs.chdir("..")

        assert vfs.getcwd() == "/a"

    def test_chdir_to_root(self):
        """chdir to / returns to root."""
        vfs = VirtualFS({})

        vfs.makedirs("a/b/c")
        vfs.chdir("a/b/c")
        vfs.chdir("/")

        assert vfs.getcwd() == "/"


class TestVFSCwdPathResolution:
    """Test that file operations respect current working directory."""

    def test_write_respects_cwd(self):
        """write resolves paths relative to CWD."""
        vfs = VirtualFS({})

        vfs.mkdir("mydir")
        vfs.chdir("mydir")
        vfs.write("file.txt", b"content")

        # File should be at /mydir/file.txt
        assert vfs.exists("/mydir/file.txt") is True
        assert vfs.read("/mydir/file.txt") == b"content"

    def test_read_respects_cwd(self):
        """read resolves paths relative to CWD."""
        vfs = VirtualFS({})

        vfs.write("mydir/file.txt", b"content")
        vfs.chdir("mydir")

        assert vfs.read("file.txt") == b"content"

    def test_exists_respects_cwd(self):
        """exists resolves paths relative to CWD."""
        vfs = VirtualFS({})

        vfs.write("mydir/file.txt", b"content")
        vfs.chdir("mydir")

        assert vfs.exists("file.txt") is True
        assert vfs.exists("nonexistent.txt") is False

    def test_list_respects_cwd(self):
        """list resolves paths relative to CWD."""
        vfs = VirtualFS({})

        vfs.write("mydir/file1.txt", b"1")
        vfs.write("mydir/file2.txt", b"2")
        vfs.chdir("mydir")

        files = vfs.list(".")
        assert sorted(files) == ["file1.txt", "file2.txt"]

    def test_remove_respects_cwd(self):
        """remove resolves paths relative to CWD."""
        vfs = VirtualFS({})

        vfs.write("mydir/file.txt", b"content")
        vfs.chdir("mydir")
        vfs.remove("file.txt")

        assert not vfs.exists("/mydir/file.txt")


class TestVFSOsPatches:
    """Test that os.* patched functions respect CWD."""

    def test_os_chdir_getcwd(self):
        """os.chdir and os.getcwd work with VFS."""
        vfs = VirtualFS({})
        vfs.mkdir("testdir")

        with patch(vfs):
            assert os.getcwd() == "/"

            os.chdir("testdir")
            assert os.getcwd() == "/testdir"

            os.chdir("/")
            assert os.getcwd() == "/"

    def test_os_mkdir_rmdir(self):
        """os.mkdir and os.rmdir work with VFS."""
        vfs = VirtualFS({})

        with patch(vfs):
            os.mkdir("newdir")
            assert os.path.isdir("newdir") is True

            os.rmdir("newdir")
            assert os.path.exists("newdir") is False

    def test_os_makedirs(self):
        """os.makedirs works with VFS."""
        vfs = VirtualFS({})

        with patch(vfs):
            os.makedirs("a/b/c", exist_ok=True)

            assert os.path.isdir("a") is True
            assert os.path.isdir("a/b") is True
            assert os.path.isdir("a/b/c") is True

    def test_os_makedirs_defaults_to_refusing_an_existing_directory(self):
        """os.makedirs() raises on a directory that is already there."""
        vfs = VirtualFS({})
        vfs.mkdir("a")

        with patch(vfs):
            with pytest.raises(FileExistsError):
                os.makedirs("a")

            os.makedirs("a", exist_ok=True)

    def test_os_path_abspath_respects_cwd(self):
        """os.path.abspath resolves against virtual CWD."""
        vfs = VirtualFS({})
        vfs.makedirs("mydir/subdir")

        with patch(vfs):
            assert os.path.abspath("file.txt") == "/file.txt"

            os.chdir("mydir")
            assert os.path.abspath("file.txt") == "/mydir/file.txt"
            assert os.path.abspath("../other.txt") == "/other.txt"


class TestAutoCreateParentDirs:
    """Test auto-creation of parent directories on file write."""

    def test_write_creates_parent_dirs(self):
        """write auto-creates parent directories."""
        vfs = VirtualFS({})

        vfs.write("a/b/c/file.txt", b"content")

        assert vfs.isdir("a") is True
        assert vfs.isdir("a/b") is True
        assert vfs.isdir("a/b/c") is True
        assert vfs.isfile("a/b/c/file.txt") is True

    def test_write_with_cwd_creates_parents(self):
        """write in subdirectory auto-creates parents."""
        vfs = VirtualFS({})

        vfs.mkdir("workdir")
        vfs.chdir("workdir")
        vfs.write("sub/file.txt", b"content")

        assert vfs.isdir("/workdir/sub") is True
        assert vfs.isfile("/workdir/sub/file.txt") is True


class TestImplicitDirectories:
    """Test that VirtualFS handles implicit directories correctly."""

    def test_implicit_directory_behavior(self):
        """Writing nested files creates implicit parent directories."""
        vfs = VirtualFS({})

        vfs.write("a/b/c.py", b"print('hello')")

        # Parents exist implicitly
        assert vfs.exists("a/b/c.py")
        assert vfs.exists("a/b")
        assert vfs.exists("a")
        assert vfs.exists(".")
        assert vfs.exists("/")

        # Type checks
        assert vfs.isdir("a")
        assert vfs.isdir("a/b")
        assert not vfs.isdir("a/b/c.py")
        assert vfs.isfile("a/b/c.py")
        assert not vfs.isfile("a/b")
        assert not vfs.isfile("a")

        # Listing
        assert vfs.list("/") == ["a"]
        assert vfs.list("a") == ["b"]
        assert vfs.list("a/b") == ["c.py"]

        # Normalization
        assert vfs.exists("./a/b/c.py")
        assert vfs.exists("/a/b/c.py")

    def test_list_multiple_items(self):
        """Listing works with multiple items in same directory."""
        vfs = VirtualFS({})

        vfs.write("pkg/__init__.py", b"")
        vfs.write("pkg/utils.py", b"")
        vfs.write("pkg/sub/mod.py", b"")
        vfs.write("top.py", b"")

        assert vfs.list("/") == ["pkg", "top.py"]
        assert vfs.list("pkg") == ["__init__.py", "sub", "utils.py"]
        assert vfs.list("pkg/sub") == ["mod.py"]


# -- list_detailed().name is the entry's own name at every depth ---------------


def _nested_tree(fs):
    fs.makedirs("/probe/sub")
    fs.write("/probe/one.txt", b"1")
    fs.write("/probe/sub/two.txt", b"2")
    return fs


def _names_and_paths(fs):
    return sorted((i.name, i.path) for i in fs.list_detailed("/probe", recursive=True))


def test_virtualfs_recursive_listing_names_are_basenames():
    from monkeyfs import VirtualFS

    assert _names_and_paths(_nested_tree(VirtualFS({}))) == [
        ("one.txt", "/probe/one.txt"),
        ("sub", "/probe/sub"),
        ("two.txt", "/probe/sub/two.txt"),
    ]


def test_mountfs_recursive_listing_names_are_basenames():
    from monkeyfs import MountFS, VirtualFS

    assert _names_and_paths(_nested_tree(MountFS(VirtualFS({})))) == [
        ("one.txt", "/probe/one.txt"),
        ("sub", "/probe/sub"),
        ("two.txt", "/probe/sub/two.txt"),
    ]


def test_isolatedfs_recursive_listing_names_are_basenames(tmp_path):
    from monkeyfs import IsolatedFS

    assert _names_and_paths(_nested_tree(IsolatedFS(str(tmp_path)))) == [
        ("one.txt", "/probe/one.txt"),
        ("sub", "/probe/sub"),
        ("two.txt", "/probe/sub/two.txt"),
    ]
