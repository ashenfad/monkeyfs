"""Tests for pathlib integration with VirtualFS patching."""

import os
from pathlib import Path

import pytest

from monkeyfs import IsolatedFS, VirtualFS, patch


class TestPathlibIntegration:
    """Test pathlib.Path integration with filesystem patching."""

    def test_path_read_text_vfs(self):
        """Test Path.read_text() reads from VFS via io.open patch."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"virtual content")

        with patch(vfs):
            p = Path("test.txt")
            assert p.exists()
            assert p.is_file()
            assert p.read_text() == "virtual content"
            assert p.read_bytes() == b"virtual content"

    def test_path_read_text_isolated(self, tmp_path):
        """Test Path.read_text() reads from IsolatedFS."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "test.txt").write_text("isolated content")

        fs = IsolatedFS(root=str(root), state={})

        with patch(fs):
            p = Path("test.txt")
            assert p.exists()
            assert p.read_text() == "isolated content"

            # Test chroot behavior
            p_root = Path("/")
            assert (p_root / "test.txt").read_text() == "isolated content"

    def test_path_iterdir_vfs(self):
        """Test Path.iterdir() lists VFS files via os.scandir patch."""
        vfs = VirtualFS({})
        vfs.write("file1.txt", b"c1")
        vfs.write("subdir/file2.txt", b"c2")

        with patch(vfs):
            # List root
            items = list(Path(".").iterdir())
            names = sorted([p.name for p in items])
            assert "file1.txt" in names
            assert "subdir" in names

            # List subdir
            items_sub = list(Path("subdir").iterdir())
            names_sub = sorted([p.name for p in items_sub])
            assert names_sub == ["file2.txt"]

    def test_path_iterdir_isolated(self, tmp_path):
        """Test Path.iterdir() lists IsolatedFS files via os.scandir patch."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "file1.txt").touch()
        (root / "subdir").mkdir()
        (root / "subdir" / "file2.txt").touch()

        fs = IsolatedFS(root=str(root), state={})

        with patch(fs):
            # List root
            items = list(Path(".").iterdir())
            names = sorted([p.name for p in items])
            assert "file1.txt" in names
            assert "subdir" in names

            # List root via absolute path (chroot check)
            items_slash = list(Path("/").iterdir())
            names_slash = sorted([p.name for p in items_slash])
            assert "file1.txt" in names_slash
            assert "subdir" in names_slash

    def test_glob_vfs(self):
        """Test Path.glob() works with VFS."""
        vfs = VirtualFS({})
        vfs.write("a.py", b"")
        vfs.write("b.txt", b"")
        vfs.write("sub/c.py", b"")

        with patch(vfs):
            # Simple glob
            py_files = sorted([p.name for p in Path(".").glob("*.py")])
            assert py_files == ["a.py"]

            # Recursive glob
            all_py = sorted([p.name for p in Path(".").rglob("*.py")])
            assert all_py == ["a.py", "c.py"]

    def test_system_path_passthrough(self):
        """Test that system paths are still accessible via pathlib."""
        # Using a known safe system path
        import sys

        sys_path = Path(sys.executable)

        vfs = VirtualFS({})  # Empty VFS

        with patch(vfs):
            # Should still exist and be readable
            assert sys_path.exists()
            assert sys_path.is_file()
            # We assume python executable is readable
            with open(sys_path, "rb") as f:
                assert f.read(4)  # Just read a few bytes

    def test_stat_isolated(self, tmp_path):
        """Test os.stat() return type and attributes in IsolatedFS."""
        root = tmp_path / "root"
        root.mkdir()
        (root / "stat_test.txt").write_text("content")

        fs = IsolatedFS(root=str(root), state={})

        with patch(fs):
            p = Path("stat_test.txt")
            st = p.stat()

            # Must be os.stat_result for full compatibility
            assert isinstance(st, os.stat_result)
            # Must have st_size
            assert hasattr(st, "st_size")
            assert st.st_size == len("content")

            # Check os.stat directly too
            os_st = os.stat("stat_test.txt")
            assert isinstance(os_st, os.stat_result)
            assert os_st.st_size == len("content")

    def test_unlink_isolated_resolved(self, tmp_path):
        """Test unlink() with resolved absolute paths in IsolatedFS (regression test)."""
        root = tmp_path / "root"
        root.mkdir()

        fs = IsolatedFS(root=str(root), state={})

        with patch(fs):
            # Create file
            p = Path("delete_me.txt")
            p.write_text("content")
            assert p.exists()

            # Resolve to absolute path using the *real* root path
            abs_p = Path(str(root / "delete_me.txt"))
            assert abs_p.is_absolute()

            # Verify absolute path works for read (fails if double-rooted)
            assert abs_p.read_text() == "content"

            # Unlink using absolute path
            abs_p.unlink()

            assert not p.exists()
            assert not abs_p.exists()
            assert not (root / "delete_me.txt").exists()

    def test_unlink_vfs(self):
        """Test unlink() works with VFS (via os.unlink patch)."""
        vfs = VirtualFS({})
        vfs.write("vfs_delete.txt", b"content")

        with patch(vfs):
            p = Path("vfs_delete.txt")
            assert p.exists()

            p.unlink()

            assert not p.exists()
            assert not vfs.exists("vfs_delete.txt")

    def test_touch_vfs(self):
        """Test Path.touch() works with VFS."""
        vfs = VirtualFS({})

        with patch(vfs):
            p = Path("touch_me.txt")
            assert not p.exists()

            # 1. Create new file
            p.touch()
            assert p.exists()
            assert p.read_text() == ""
            assert vfs.read("touch_me.txt") == b""

            # 2. Touch existing (should not error)
            p.touch()
            assert p.exists()

            # 3. Touch with exist_ok=False (should fail)
            with pytest.raises(FileExistsError):
                p.touch(exist_ok=False)

    def test_stat_vfs_pathlib(self):
        """Test pathlib.Path.stat() returns usable os.stat_result in VFS."""
        vfs = VirtualFS({})
        vfs.write("leo.ics", b"data")

        with patch(vfs):
            p = Path("leo.ics")
            s = p.stat()

            # Verify type and attribute access
            assert isinstance(s, os.stat_result)
            assert s.st_size == 4
            assert s.st_mode > 0

    def test_getcwd_virtualized(self, tmp_path):
        """Test os.getcwd() returns virtual root '/' when VFS is active."""

        # Test VirtualFS
        vfs = VirtualFS({})
        with patch(vfs):
            assert os.getcwd() == "/"
            # Path('.').resolve() calls os.getcwd()
            assert str(Path(".").resolve()) == "/"
            assert str(Path(".").absolute()) == "/"

        # Test IsolatedFS
        root = tmp_path / "root"
        root.mkdir()
        fs = IsolatedFS(root=str(root), state={})
        with patch(fs):
            assert os.getcwd() == "/"
            assert str(Path(".").resolve()) == "/"
            assert str(Path(".").absolute()) == "/"

    def test_lstat_vfs(self):
        """Test Path.lstat() and os.lstat() work with VFS."""
        vfs = VirtualFS({})
        vfs.write("file.txt", b"content")

        with patch(vfs):
            p = Path("file.txt")

            # Test Path.lstat()
            st = p.lstat()
            assert st.st_size == 7
            assert st.st_mtime > 0

            # Test os.lstat()
            st_os = os.lstat("file.txt")
            assert st_os.st_size == 7
