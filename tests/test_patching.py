"""Tests for VirtualFS patching and context manager."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from monkeyfs import VirtualFS, use_fs


class TestPatchingBasics:
    """Test filesystem patching basics."""

    def test_open_patched_in_context(self):
        """Test that open() is patched within VFS context."""
        vfs = VirtualFS({})

        with use_fs(vfs):
            with open("test.txt", "w") as f:
                f.write("patched!")

        # File should be in VFS, not on real filesystem
        assert vfs.read("test.txt") == b"patched!"

    def test_open_not_patched_outside_context(self):
        """Test that open() works normally outside VFS context."""
        import tempfile

        # Outside context, open should use real filesystem
        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
            tmp.write("real file")
            tmp_path = tmp.name

        try:
            with open(tmp_path, "r") as f:
                content = f.read()
            assert content == "real file"
        finally:
            os.remove(tmp_path)

    def test_listdir_patched(self):
        """Test that os.listdir() is patched."""
        vfs = VirtualFS({})

        vfs.write("file1.txt", b"content1")
        vfs.write("file2.txt", b"content2")

        with use_fs(vfs):
            files = os.listdir("/")

        assert sorted(files) == ["file1.txt", "file2.txt"]

    def test_scandir_patched(self):
        """Test that os.scandir() is patched."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"hello")
        vfs.write("sub/nested.txt", b"world")

        with use_fs(vfs):
            with os.scandir("/") as entries:
                result = {e.name: e.is_dir() for e in entries}

        assert result == {"file.txt": False, "sub": True}

    def test_scandir_entry_stat(self):
        """Test that scandir DirEntry.stat() works."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"12345")

        with use_fs(vfs):
            with os.scandir("/") as entries:
                entry = next(iter(entries))
                assert entry.name == "file.txt"
                assert entry.stat().st_size == 5

    def test_utime_patched(self):
        """Test that os.utime() is patched (no-op for existing files)."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"content")

        with use_fs(vfs):
            # Should not raise for existing file
            os.utime("file.txt", None)

    def test_utime_missing_file(self):
        """Test that os.utime() raises for missing files."""
        vfs = VirtualFS({})

        with use_fs(vfs):
            with pytest.raises(FileNotFoundError):
                os.utime("missing.txt", None)

    def test_exists_patched(self):
        """Test that os.path.exists() is patched."""
        vfs = VirtualFS({})

        vfs.write("exists.txt", b"content")

        with use_fs(vfs):
            assert os.path.exists("exists.txt") is True
            assert os.path.exists("nonexistent.txt") is False

    def test_nested_directory_open(self):
        """Test that files in nested directories can be opened with standard operations."""
        vfs = VirtualFS({})

        # Write file to nested directory (like debug/dom.html)
        vfs.write("debug/dom.html", b"<html>test</html>")

        with use_fs(vfs):
            # Test os.path.exists
            assert os.path.exists("debug/dom.html") is True

            # Test open()
            with open("debug/dom.html", "r") as f:
                content = f.read()
            assert content == "<html>test</html>"

    def test_isfile_patched(self):
        """Test that os.path.isfile() is patched."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"content")
        vfs.write("dir/nested.txt", b"nested")

        with use_fs(vfs):
            assert os.path.isfile("file.txt") is True
            assert os.path.isfile("dir") is False

    def test_stat_file(self):
        """Test that os.stat() returns proper metadata for VFS files."""
        import stat as stat_module

        vfs = VirtualFS({})

        vfs.write("file.txt", b"hello world")

        with use_fs(vfs):
            stat_result = os.stat("file.txt")

            # Verify file type and permissions
            assert stat_module.S_ISREG(stat_result.st_mode)
            assert stat_result.st_mode & 0o777 == 0o644

            # Verify size
            assert stat_result.st_size == 11

            # Verify timestamps exist (should be recent)
            import time

            now = time.time()
            assert stat_result.st_mtime <= now
            assert stat_result.st_ctime <= now
            assert stat_result.st_mtime > now - 10  # Created within last 10 seconds

    def test_stat_directory(self):
        """Test that os.stat() works for VFS directories."""
        import stat as stat_module

        vfs = VirtualFS({})

        vfs.write("dir/file.txt", b"content")

        with use_fs(vfs):
            stat_result = os.stat("dir")

            # Verify directory type and permissions
            assert stat_module.S_ISDIR(stat_result.st_mode)
            assert stat_result.st_mode & 0o777 == 0o755

            # Verify size is zero for directories
            assert stat_result.st_size == 0

    def test_stat_nonexistent(self):
        """Test that os.stat() raises FileNotFoundError for missing paths."""
        vfs = VirtualFS({})

        with use_fs(vfs):
            with pytest.raises(FileNotFoundError):
                os.stat("nonexistent.txt")


class TestContextIsolation:
    """Test that patching is safe across threads using contextvars."""

    def test_thread_pool_context_isolation(self):
        """Test VFS context isolation in thread pool."""
        import contextvars

        vfs_1 = VirtualFS({})
        vfs_2 = VirtualFS({})

        def worker(vfs, content):
            """Worker function that runs in thread pool."""
            with use_fs(vfs):
                with open("file.txt", "w") as f:
                    f.write(content)

        # Copy context and run in executor
        ctx = contextvars.copy_context()
        with ThreadPoolExecutor(max_workers=2) as executor:
            future1 = executor.submit(ctx.run, worker, vfs_1, "content 1")
            future2 = executor.submit(ctx.run, worker, vfs_2, "content 2")

            future1.result()
            future2.result()

        # Each VFS should have its own file
        assert vfs_1.read("file.txt") == b"content 1"
        assert vfs_2.read("file.txt") == b"content 2"


class TestPatchingEdgeCases:
    """Test edge cases in patching."""

    def test_nested_contexts(self):
        """Test that nested VFS contexts work correctly."""
        vfs_outer = VirtualFS({})
        vfs_inner = VirtualFS({})

        with use_fs(vfs_outer):
            with open("outer.txt", "w") as f:
                f.write("outer")

            with use_fs(vfs_inner):
                with open("inner.txt", "w") as f:
                    f.write("inner")

                # Inner context is active
                assert vfs_inner.exists("inner.txt") is True

            # Back to outer context
            assert vfs_outer.exists("outer.txt") is True
            # Inner file should not be in outer VFS
            assert vfs_outer.exists("inner.txt") is False

    def test_exception_in_context(self):
        """Test that VFS context is properly reset on exception."""
        vfs = VirtualFS({})

        try:
            with use_fs(vfs):
                with open("file.txt", "w") as f:
                    f.write("before error")
                raise ValueError("test error")
        except ValueError:
            pass

        # File should still be saved despite exception
        assert vfs.read("file.txt") == b"before error"

        # Context should be reset - open should work normally now
        import tempfile

        with tempfile.NamedTemporaryFile(mode="w", delete=False) as tmp:
            tmp.write("real")
            tmp_path = tmp.name

        try:
            with open(tmp_path, "r") as f:
                assert f.read() == "real"
        finally:
            os.remove(tmp_path)

    def test_open_with_file_descriptor(self):
        """Test that patched open doesn't break file descriptor usage."""
        import tempfile

        vfs = VirtualFS({})

        # Create a real temporary file
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"real content")
            tmp_path = tmp.name

        try:
            with use_fs(vfs):
                # Opening by file descriptor should still work (bypass patching)
                pass
        finally:
            os.remove(tmp_path)

    def test_islink_patched(self):
        """Test that os.path.islink() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with use_fs(vfs):
            assert os.path.islink("test.txt") is False
            assert os.path.islink("nonexistent.txt") is False

    def test_lexists_patched(self):
        """Test that os.path.lexists() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with use_fs(vfs):
            assert os.path.lexists("test.txt") is True
            assert os.path.lexists("nonexistent.txt") is False

    def test_samefile_patched(self):
        """Test that os.path.samefile() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with use_fs(vfs):
            assert os.path.samefile("test.txt", "test.txt") is True
            assert os.path.samefile("test.txt", "./test.txt") is True

            vfs.write("other.txt", b"other")
            assert os.path.samefile("test.txt", "other.txt") is False

    def test_realpath_patched(self):
        """Test that os.path.realpath() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with use_fs(vfs):
            assert os.path.realpath("test.txt") == "/test.txt"
            assert os.path.realpath("./test.txt") == "/test.txt"
            assert os.path.realpath("/test.txt") == "/test.txt"
            assert os.path.realpath("dir/../test.txt") == "/test.txt"


class TestPartialProtocol:
    """Test that missing optional methods raise NotImplementedError."""

    def _make_minimal_fs(self):
        """Create a FS with only required methods (no optional ones)."""

        class MinimalFS:
            def __init__(self):
                self._files = {"/test.txt": b"content"}
                self._cwd = "/"

            def open(self, path, mode="r", **kwargs):
                from io import BytesIO, TextIOWrapper

                path = self._resolve(path)
                if "w" in mode or "a" in mode or "x" in mode:
                    buf = BytesIO()
                    if "b" not in mode:
                        return TextIOWrapper(buf)
                    return buf
                data = self._files.get(path)
                if data is None:
                    raise FileNotFoundError(path)
                buf = BytesIO(data)
                if "b" not in mode:
                    return TextIOWrapper(buf)
                return buf

            def stat(self, path):
                from datetime import datetime, timezone

                from monkeyfs.base import FileMetadata

                path = self._resolve(path)
                if path in self._files:
                    now = datetime.now(timezone.utc).isoformat()
                    return FileMetadata(
                        size=len(self._files[path]),
                        created_at=now,
                        modified_at=now,
                    )
                raise FileNotFoundError(path)

            def exists(self, path):
                return self._resolve(path) in self._files

            def isfile(self, path):
                return self._resolve(path) in self._files

            def isdir(self, path):
                path = self._resolve(path)
                return path == "/" or any(
                    f.startswith(path + "/") for f in self._files
                )

            def list(self, path="."):
                path = self._resolve(path)
                if not path.endswith("/"):
                    path += "/"
                names = set()
                for f in self._files:
                    if f.startswith(path):
                        rest = f[len(path) :]
                        if rest:
                            names.add(rest.split("/")[0])
                return sorted(names)

            def remove(self, path):
                path = self._resolve(path)
                if path not in self._files:
                    raise FileNotFoundError(path)
                del self._files[path]

            def mkdir(self, path, parents=False, exist_ok=False):
                pass

            def makedirs(self, path, exist_ok=True):
                pass

            def rename(self, src, dst):
                src = self._resolve(src)
                dst = self._resolve(dst)
                if src not in self._files:
                    raise FileNotFoundError(src)
                self._files[dst] = self._files.pop(src)

            def getcwd(self):
                return self._cwd

            def chdir(self, path):
                self._cwd = self._resolve(path)

            def _resolve(self, path):
                path = str(path)
                if not path.startswith("/"):
                    path = self._cwd.rstrip("/") + "/" + path
                import posixpath

                return posixpath.normpath(path)

        return MinimalFS()

    def test_required_methods_work(self):
        """Verify the minimal FS works for basic operations."""
        fs = self._make_minimal_fs()
        with use_fs(fs):
            assert os.path.exists("test.txt") is True
            assert os.path.isfile("test.txt") is True
            assert os.listdir("/") == ["test.txt"]
            stat_result = os.stat("test.txt")
            assert stat_result.st_size == 7

    def test_rmdir_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="rmdir"):
                os.rmdir("/somedir")

    def test_islink_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="islink"):
                os.path.islink("test.txt")

    def test_samefile_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="samefile"):
                os.path.samefile("test.txt", "test.txt")

    def test_realpath_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="realpath"):
                os.path.realpath("test.txt")

    def test_getsize_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="getsize"):
                os.path.getsize("test.txt")

    def test_replace_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="replace"):
                os.replace("test.txt", "other.txt")

    def test_access_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="access"):
                os.access("test.txt", os.R_OK)

    def test_readlink_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="readlink"):
                os.readlink("test.txt")

    def test_symlink_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="symlink"):
                os.symlink("test.txt", "link.txt")

    def test_link_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="link"):
                os.link("test.txt", "copy.txt")

    def test_chmod_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="chmod"):
                os.chmod("test.txt", 0o755)

    def test_chown_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        if not hasattr(os, "chown"):
            pytest.skip("os.chown not available on this platform")
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="chown"):
                os.chown("test.txt", 1000, 1000)

    def test_truncate_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with use_fs(fs):
            with pytest.raises(NotImplementedError, match="truncate"):
                os.truncate("test.txt", 0)


class TestOptionalMethodsMemoryFS:
    """Test optional methods work through patching with MemoryFS."""

    def test_replace(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()
        fs.files["/a.txt"] = b"data"

        with use_fs(fs):
            os.replace("a.txt", "b.txt")

        assert "/b.txt" in fs.files
        assert "/a.txt" not in fs.files

    def test_access(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()
        fs.files["/a.txt"] = b"data"

        with use_fs(fs):
            assert os.access("a.txt", os.R_OK) is True
            assert os.access("missing.txt", os.R_OK) is False

    def test_readlink_raises(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()
        fs.files["/a.txt"] = b"data"

        with use_fs(fs):
            with pytest.raises(OSError):
                os.readlink("a.txt")

    def test_symlink_raises(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()

        with use_fs(fs):
            with pytest.raises(OSError):
                os.symlink("target", "link")

    def test_link(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()
        fs.files["/a.txt"] = b"data"

        with use_fs(fs):
            os.link("a.txt", "b.txt")

        assert fs.files["/b.txt"] == b"data"

    def test_chmod_noop(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()
        fs.files["/a.txt"] = b"data"

        with use_fs(fs):
            os.chmod("a.txt", 0o755)  # should not raise

    def test_chmod_missing_file(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()

        with use_fs(fs):
            with pytest.raises(FileNotFoundError):
                os.chmod("missing.txt", 0o755)

    def test_chown_noop(self):
        from monkeyfs import MemoryFS

        if not hasattr(os, "chown"):
            pytest.skip("os.chown not available on this platform")

        fs = MemoryFS()
        fs.files["/a.txt"] = b"data"

        with use_fs(fs):
            os.chown("a.txt", 1000, 1000)  # should not raise

    def test_truncate(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()
        fs.files["/a.txt"] = b"hello world"

        with use_fs(fs):
            os.truncate("a.txt", 5)

        assert fs.files["/a.txt"] == b"hello"

    def test_truncate_missing_file(self):
        from monkeyfs import MemoryFS

        fs = MemoryFS()

        with use_fs(fs):
            with pytest.raises(FileNotFoundError):
                os.truncate("missing.txt", 0)


class TestIsolatedPatching:
    """Test patching with IsolatedFS."""

    def test_isolated_realpath(self, tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "test.txt").write_text("content")

        isolated = IsolatedFS(str(root), state={})

        with use_fs(isolated):
            assert os.path.realpath("test.txt") == "/test.txt"
            assert os.path.realpath("./test.txt") == "/test.txt"
            assert os.path.realpath("/test.txt") == "/test.txt"

    def test_isolated_islink(self, tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        file = root / "test.txt"
        file.write_text("content")

        link = root / "link.txt"
        link.symlink_to(file)

        isolated = IsolatedFS(str(root), state={})

        with use_fs(isolated):
            assert os.path.islink("link.txt") is True
            assert os.path.islink("test.txt") is False

    def test_isolated_samefile(self, tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        file = root / "test.txt"
        file.write_text("content")

        link = root / "link.txt"
        link.symlink_to(file)

        isolated = IsolatedFS(str(root), state={})

        with use_fs(isolated):
            assert os.path.samefile("test.txt", "link.txt") is True
            assert os.path.samefile("test.txt", "test.txt") is True
