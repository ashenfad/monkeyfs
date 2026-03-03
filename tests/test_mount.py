"""Tests for MountFS."""

import errno
import os

import pytest

from monkeyfs import VirtualFS, patch
from monkeyfs.mount import MountFS
from monkeyfs.readonly import ReadOnlyFS


def _make_base():
    """Create a VirtualFS with some base files."""
    vfs = VirtualFS({})
    vfs.write("app/main.py", b"print('hi')")
    vfs.write("app/utils.py", b"def add(a,b): return a+b")
    vfs.write("readme.txt", b"hello")
    return vfs


def _make_mount():
    """Create a VirtualFS with some mount files."""
    vfs = VirtualFS({})
    vfs.write("summary.md", b"# Data exploration")
    vfs.write("events/001-prompt.md", b"user: explore")
    vfs.write("events/002-action.md", b"agent: read schema")
    return vfs


class TestMountFSRouting:
    """Operations should route to the correct filesystem."""

    def test_read_from_base(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        assert fs.read("/readme.txt") == b"hello"

    def test_read_from_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        assert fs.read("/chapters/summary.md") == b"# Data exploration"

    def test_read_nested_in_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        assert fs.read("/chapters/events/001-prompt.md") == b"user: explore"

    def test_write_to_base(self):
        base = _make_base()
        fs = MountFS(base, {"/chapters": _make_mount()})
        fs.write("/new.txt", b"new file")
        assert base.read("new.txt") == b"new file"

    def test_write_to_mount(self):
        mount = _make_mount()
        fs = MountFS(_make_base(), {"/chapters": mount})
        fs.write("/chapters/new.md", b"new chapter")
        assert mount.read("new.md") == b"new chapter"

    def test_multiple_mounts(self):
        mount1 = VirtualFS({})
        mount1.write("a.txt", b"mount1")
        mount2 = VirtualFS({})
        mount2.write("b.txt", b"mount2")
        fs = MountFS(_make_base(), {"/m1": mount1, "/m2": mount2})
        assert fs.read("/m1/a.txt") == b"mount1"
        assert fs.read("/m2/b.txt") == b"mount2"

    def test_nested_mounts(self):
        """Deeper mount takes priority over shallower."""
        outer = VirtualFS({})
        outer.write("outer.txt", b"outer")
        inner = VirtualFS({})
        inner.write("inner.txt", b"inner")
        fs = MountFS(_make_base(), {"/a": outer, "/a/b": inner})
        assert fs.read("/a/outer.txt") == b"outer"
        assert fs.read("/a/b/inner.txt") == b"inner"

    def test_mount_and_unmount(self):
        mount = _make_mount()
        fs = MountFS(_make_base())
        # Not mounted yet
        assert not fs.exists("/chapters/summary.md")
        # Mount
        fs.mount("/chapters", mount)
        assert fs.read("/chapters/summary.md") == b"# Data exploration"
        # Unmount
        fs.unmount("/chapters")
        assert not fs.exists("/chapters/summary.md")

    def test_base_unchanged_after_mount_write(self):
        base = _make_base()
        mount = _make_mount()
        fs = MountFS(base, {"/chapters": mount})
        fs.write("/chapters/new.md", b"new")
        assert not base.exists("chapters/new.md")

    def test_cannot_mount_at_root(self):
        with pytest.raises(ValueError, match="Cannot mount"):
            MountFS(_make_base(), {"/": VirtualFS({})})


class TestMountFSDirectoryListing:
    """list() should merge entries from base and mounts."""

    def test_list_root_shows_mount_points(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list("/")
        assert "chapters" in entries
        assert "app" in entries
        assert "readme.txt" in entries

    def test_list_mount_root(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list("/chapters")
        assert "summary.md" in entries
        assert "events" in entries

    def test_list_inside_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list("/chapters/events")
        assert "001-prompt.md" in entries
        assert "002-action.md" in entries

    def test_list_recursive_across_mounts(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list("/", recursive=True)
        assert "app/main.py" in entries
        assert "chapters/summary.md" in entries
        assert "chapters/events/001-prompt.md" in entries

    def test_list_recursive_within_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list("/chapters", recursive=True)
        assert "summary.md" in entries
        assert "events/001-prompt.md" in entries

    def test_list_detailed_includes_mounts(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list_detailed("/")
        names = [e.name for e in entries]
        assert "chapters" in names
        chapter_entry = [e for e in entries if e.name == "chapters"][0]
        assert chapter_entry.is_dir

    def test_list_detailed_uses_real_metadata(self):
        """list_detailed should use actual stat metadata, not synthesized."""
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        entries = fs.list_detailed("/")
        file_entry = [e for e in entries if e.name == "readme.txt"][0]
        assert file_entry.size == 5
        assert not file_entry.is_dir
        assert file_entry.created_at is not None

    def test_isdir_mount_point(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        assert fs.isdir("/chapters")

    def test_isdir_implicit_parent(self):
        """A mount at /a/b makes /a appear as a directory."""
        fs = MountFS(_make_base(), {"/deep/nested": _make_mount()})
        assert fs.isdir("/deep")

    def test_exists_mount_point(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        assert fs.exists("/chapters")

    def test_isfile_mount_point_false(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        assert not fs.isfile("/chapters")


class TestMountFSCwd:
    """CWD should be managed by MountFS, not delegated."""

    def test_default_cwd(self):
        fs = MountFS(_make_base())
        assert fs.getcwd() == "/"

    def test_chdir_in_base(self):
        fs = MountFS(_make_base())
        fs.chdir("/app")
        assert fs.getcwd() == "/app"

    def test_chdir_into_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        fs.chdir("/chapters")
        assert fs.getcwd() == "/chapters"

    def test_relative_path_in_base(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        fs.chdir("/app")
        assert fs.read("main.py") == b"print('hi')"

    def test_relative_path_in_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        fs.chdir("/chapters")
        assert fs.read("summary.md") == b"# Data exploration"

    def test_chdir_nonexistent_raises(self):
        fs = MountFS(_make_base())
        with pytest.raises(FileNotFoundError):
            fs.chdir("/nonexistent")

    def test_chdir_into_implicit_parent(self):
        """Can chdir into implicit parent of a mount."""
        fs = MountFS(_make_base(), {"/deep/nested": _make_mount()})
        fs.chdir("/deep")
        assert fs.getcwd() == "/deep"


class TestMountFSStat:
    """stat() should handle base files, mount files, and mount points."""

    def test_stat_base_file(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        meta = fs.stat("/readme.txt")
        assert meta.size == 5
        assert not meta.is_dir

    def test_stat_mount_file(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        meta = fs.stat("/chapters/summary.md")
        assert meta.size == len(b"# Data exploration")
        assert not meta.is_dir

    def test_stat_mount_point(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        meta = fs.stat("/chapters")
        assert meta.is_dir

    def test_stat_implicit_parent(self):
        fs = MountFS(_make_base(), {"/deep/nested": _make_mount()})
        meta = fs.stat("/deep")
        assert meta.is_dir


class TestMountFSCrossMount:
    """Cross-mount operations should handle correctly."""

    def test_rename_same_mount(self):
        mount = _make_mount()
        fs = MountFS(_make_base(), {"/chapters": mount})
        fs.rename("/chapters/summary.md", "/chapters/overview.md")
        assert fs.read("/chapters/overview.md") == b"# Data exploration"
        assert not fs.exists("/chapters/summary.md")

    def test_rename_cross_mount_file(self):
        """Cross-mount file rename should copy+remove."""
        mount = _make_mount()
        base = _make_base()
        fs = MountFS(base, {"/chapters": mount})
        fs.rename("/chapters/summary.md", "/summary_copy.md")
        assert fs.read("/summary_copy.md") == b"# Data exploration"
        assert not fs.exists("/chapters/summary.md")

    def test_rename_cross_mount_directory_raises(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        with pytest.raises(OSError) as exc_info:
            fs.rename("/chapters/events", "/events_copy")
        assert exc_info.value.errno == errno.EXDEV

    def test_samefile_cross_mount_false(self):
        mount = _make_mount()
        base = VirtualFS({})
        base.write("same.md", b"# Data exploration")
        fs = MountFS(base, {"/chapters": mount})
        assert not fs.samefile("/same.md", "/chapters/summary.md")

    def test_samefile_same_mount(self):
        base = _make_base()
        fs = MountFS(base)
        assert fs.samefile("/readme.txt", "/readme.txt")


class TestMountFSReadOnlyMount:
    """The chapters use case: writable base + read-only mount."""

    def test_readonly_mount_read(self):
        mount = ReadOnlyFS(_make_mount())
        fs = MountFS(_make_base(), {"/chapters": mount})
        assert fs.read("/chapters/summary.md") == b"# Data exploration"

    def test_readonly_mount_write_blocked(self):
        mount = ReadOnlyFS(_make_mount())
        fs = MountFS(_make_base(), {"/chapters": mount})
        with pytest.raises(PermissionError):
            fs.write("/chapters/new.md", b"nope")

    def test_writable_base_with_readonly_mount(self):
        """Base allows writes even when mount is read-only."""
        mount = ReadOnlyFS(_make_mount())
        base = _make_base()
        fs = MountFS(base, {"/chapters": mount})
        fs.write("/new_file.txt", b"works")
        assert fs.read("/new_file.txt") == b"works"


class TestMountFSWithPatch:
    """MountFS should work through the patch() context."""

    def test_patched_open_routes_to_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        with patch(fs):
            with open("/chapters/summary.md", "r") as f:
                assert f.read() == "# Data exploration"

    def test_patched_open_routes_to_base(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        with patch(fs):
            with open("/readme.txt", "r") as f:
                assert f.read() == "hello"

    def test_patched_listdir_merges(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        with patch(fs):
            entries = os.listdir("/")
            assert "chapters" in entries
            assert "app" in entries

    def test_patched_stat_mount_file(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        with patch(fs):
            result = os.stat("/chapters/summary.md")
            assert result.st_size == len(b"# Data exploration")

    def test_patched_readonly_mount_write_blocked(self):
        mount = ReadOnlyFS(_make_mount())
        fs = MountFS(_make_base(), {"/chapters": mount})
        with patch(fs):
            with pytest.raises(PermissionError):
                with open("/chapters/new.md", "w") as f:
                    f.write("nope")


class TestMountFSGlob:
    """glob() should work across mount boundaries."""

    def test_glob_in_base(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        matches = fs.glob("/app/*.py")
        assert "/app/main.py" in matches
        assert "/app/utils.py" in matches

    def test_glob_in_mount(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        matches = fs.glob("/chapters/events/*.md")
        assert "/chapters/events/001-prompt.md" in matches
        assert "/chapters/events/002-action.md" in matches

    def test_glob_mount_root(self):
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        matches = fs.glob("/chapters/*.md")
        assert "/chapters/summary.md" in matches

    def test_glob_relative_with_cwd(self):
        """Relative glob patterns should resolve against MountFS CWD."""
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        fs.chdir("/app")
        matches = fs.glob("*.py")
        assert "/app/main.py" in matches
        assert "/app/utils.py" in matches

    def test_glob_relative_in_mount(self):
        """Relative glob inside a mount should resolve against CWD."""
        fs = MountFS(_make_base(), {"/chapters": _make_mount()})
        fs.chdir("/chapters")
        matches = fs.glob("*.md")
        assert "/chapters/summary.md" in matches

    def test_glob_base_shadowed_by_mount(self):
        """Base FS paths under mount prefixes should not appear in results."""
        base = _make_base()
        # Write a file in base under the mount prefix path
        base.write("chapters/ghost.md", b"should be hidden")
        mount = _make_mount()
        fs = MountFS(base, {"/chapters": mount})
        matches = fs.glob("/chapters/*.md")
        assert "/chapters/summary.md" in matches
        assert "/chapters/ghost.md" not in matches
