"""Tests for VirtualFS patching and context manager."""

import asyncio
import errno
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from monkeyfs import VirtualFS, patch


class TestPatchingBasics:
    """Test filesystem patching basics."""

    def test_open_patched_in_context(self):
        """Test that open() is patched within VFS context."""
        vfs = VirtualFS({})

        with patch(vfs):
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

        with patch(vfs):
            files = os.listdir("/")

        assert sorted(files) == ["file1.txt", "file2.txt"]

    def test_scandir_patched(self):
        """Test that os.scandir() is patched."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"hello")
        vfs.write("sub/nested.txt", b"world")

        with patch(vfs):
            with os.scandir("/") as entries:
                result = {e.name: e.is_dir() for e in entries}

        assert result == {"file.txt": False, "sub": True}

    def test_scandir_entry_stat(self):
        """Test that scandir DirEntry.stat() works."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"12345")

        with patch(vfs):
            with os.scandir("/") as entries:
                entry = next(iter(entries))
                assert entry.name == "file.txt"
                assert entry.stat().st_size == 5

    def test_utime_patched(self):
        """Test that os.utime() updates VFS metadata."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"content")

        with patch(vfs):
            os.utime("file.txt", (1577836800, 1577836800))

        meta = vfs.stat("file.txt")
        assert "2020-01-01" in meta.modified_at

    def test_utime_missing_file(self):
        """Test that os.utime() raises for missing files."""
        vfs = VirtualFS({})

        with patch(vfs):
            with pytest.raises(FileNotFoundError):
                os.utime("missing.txt", None)

    def test_exists_patched(self):
        """Test that os.path.exists() is patched."""
        vfs = VirtualFS({})

        vfs.write("exists.txt", b"content")

        with patch(vfs):
            assert os.path.exists("exists.txt") is True
            assert os.path.exists("nonexistent.txt") is False

    def test_nested_directory_open(self):
        """Test that files in nested directories can be opened with standard operations."""
        vfs = VirtualFS({})

        # Write file to nested directory (like debug/dom.html)
        vfs.write("debug/dom.html", b"<html>test</html>")

        with patch(vfs):
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

        with patch(vfs):
            assert os.path.isfile("file.txt") is True
            assert os.path.isfile("dir") is False

    def test_stat_file(self):
        """Test that os.stat() returns proper metadata for VFS files."""
        import stat as stat_module

        vfs = VirtualFS({})

        vfs.write("file.txt", b"hello world")

        with patch(vfs):
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

        with patch(vfs):
            stat_result = os.stat("dir")

            # Verify directory type and permissions
            assert stat_module.S_ISDIR(stat_result.st_mode)
            assert stat_result.st_mode & 0o777 == 0o755

            # Verify size is zero for directories
            assert stat_result.st_size == 0

    def test_stat_nonexistent(self):
        """Test that os.stat() raises FileNotFoundError for missing paths."""
        vfs = VirtualFS({})

        with patch(vfs):
            with pytest.raises(FileNotFoundError):
                os.stat("nonexistent.txt")


class TestPathTypeNormalization:
    """Every path type the stdlib accepts must route through the VFS.

    Matching only str/Path let bytes and non-Path os.PathLike arguments skip
    interception entirely and reach the host filesystem -- a read and write
    bypass needing nothing but a bytes literal.
    """

    class Fspath:
        """A non-Path os.PathLike, as returned by many libraries."""

        def __init__(self, p):
            self.p = p

        def __fspath__(self):
            return self.p

    @pytest.fixture
    def vfs(self):
        fs = VirtualFS({})
        fs.write("/f.txt", b"virtual content")
        return fs

    @pytest.mark.parametrize(
        "make_path",
        [
            pytest.param(lambda p: os.fsencode(p), id="bytes"),
            pytest.param(lambda p: TestPathTypeNormalization.Fspath(p), id="pathlike"),
        ],
    )
    def test_host_file_not_reachable(self, vfs, make_path):
        """A host path absent from the VFS must not resolve to the host."""
        host_file = "/etc/hosts"
        assert os.path.isfile(host_file), "test needs a readable host file"

        with patch(vfs):
            with pytest.raises(FileNotFoundError):
                os.open(make_path(host_file), os.O_RDONLY)
            with pytest.raises(FileNotFoundError):
                open(make_path(host_file), "rb")

    @pytest.mark.parametrize(
        "make_path",
        [
            pytest.param(lambda p: os.fsencode(p), id="bytes"),
            pytest.param(lambda p: TestPathTypeNormalization.Fspath(p), id="pathlike"),
        ],
    )
    def test_reads_route_to_vfs(self, vfs, make_path):
        with patch(vfs):
            fd = os.open(make_path("/f.txt"), os.O_RDONLY)
            try:
                assert os.read(fd, 100) == b"virtual content"
            finally:
                os.close(fd)
            with open(make_path("/f.txt"), "rb") as f:
                assert f.read() == b"virtual content"

    def test_writes_land_in_vfs_not_host(self, vfs, tmp_path):
        """A bytes write to a host-looking path must stay inside the VFS."""
        target = str(tmp_path / "written.txt")
        vfs.makedirs(str(tmp_path))  # patched open() needs parents, matching POSIX
        with patch(vfs):
            with open(os.fsencode(target), "w") as f:
                f.write("from the vfs")

        assert not os.path.exists(target), "bytes path escaped onto the host"
        assert vfs.read(target) == b"from the vfs"

    def test_directory_refusal_covers_bytes(self, vfs):
        """The directory-fd refusal must not be skippable via a bytes path."""
        with patch(vfs):
            with pytest.raises(IsADirectoryError) as exc:
                os.open(os.fsencode(sys.prefix), os.O_RDONLY)
            assert exc.value.errno == errno.EISDIR


class TestDirFdRejection:
    """fd-relative path resolution must fail loudly while a VFS is active.

    A dir_fd names a host kernel object, so the operation resolves against the
    real filesystem regardless of the active VFS. Honoring it escapes the
    filesystem boundary; dropping it silently retargets the call at a
    different directory than the caller named.
    """

    @pytest.fixture
    def host_dir_fd(self):
        """A real directory fd opened before patching.

        Obtaining one from *inside* a patched context is refused outright
        (see TestDirectoryFdRefusal), so this models an fd inherited from
        before patch() or handed in by host code -- the remaining way one
        can be present while a filesystem is active.
        """
        fd = os.open(sys.prefix, os.O_RDONLY)
        try:
            yield fd
        finally:
            os.close(fd)

    def test_open_with_dir_fd_cannot_write_to_host(self, host_dir_fd):
        """The escape chain: host dir fd -> dir_fd write -> host file."""
        probe_name = "monkeyfs-dir-fd-test-probe.txt"
        probe = os.path.join(sys.prefix, probe_name)
        assert not os.path.exists(probe)

        vfs = VirtualFS({})
        try:
            with patch(vfs):
                with pytest.raises(OSError) as exc:
                    os.open(
                        probe_name, os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=host_dir_fd
                    )
                assert exc.value.errno == errno.ENOTSUP
            assert not os.path.exists(probe), "dir_fd escaped the VFS onto the host"
        finally:
            if os.path.exists(probe):
                os.remove(probe)

    def test_rmdir_with_dir_fd_does_not_reach_host(self, host_dir_fd):
        vfs = VirtualFS({})
        with patch(vfs):
            with pytest.raises(OSError) as exc:
                os.rmdir("anything", dir_fd=host_dir_fd)
            assert exc.value.errno == errno.ENOTSUP

    @pytest.mark.parametrize(
        "call",
        [
            pytest.param(lambda fd: os.mkdir("x", dir_fd=fd), id="mkdir"),
            pytest.param(lambda fd: os.stat("x", dir_fd=fd), id="stat"),
            pytest.param(lambda fd: os.lstat("x", dir_fd=fd), id="lstat"),
            pytest.param(lambda fd: os.unlink("x", dir_fd=fd), id="unlink"),
            pytest.param(lambda fd: os.remove("x", dir_fd=fd), id="remove"),
            pytest.param(lambda fd: os.chmod("x", 0o644, dir_fd=fd), id="chmod"),
            pytest.param(lambda fd: os.utime("x", None, dir_fd=fd), id="utime"),
            pytest.param(lambda fd: os.readlink("x", dir_fd=fd), id="readlink"),
            pytest.param(lambda fd: os.symlink("x", "y", dir_fd=fd), id="symlink"),
            pytest.param(lambda fd: os.rename("x", "y", src_dir_fd=fd), id="rename"),
            pytest.param(lambda fd: os.replace("x", "y", dst_dir_fd=fd), id="replace"),
            pytest.param(lambda fd: os.link("x", "y", src_dir_fd=fd), id="link"),
        ],
    )
    def test_dir_fd_rejected_uniformly(self, call, host_dir_fd):
        """No patched operation may silently drop or honor a dir_fd."""
        vfs = VirtualFS({})
        vfs.write("x", b"content")
        with patch(vfs):
            with pytest.raises(OSError) as exc:
                call(host_dir_fd)
            assert exc.value.errno == errno.ENOTSUP

    def test_dir_fd_still_works_outside_patch_context(self, tmp_path):
        """Rejection is scoped to an active VFS -- real dir_fd use is untouched."""
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            os.mkdir("real", dir_fd=fd)
            assert (tmp_path / "real").is_dir()
            os.rmdir("real", dir_fd=fd)

            wfd = os.open("f.txt", os.O_CREAT | os.O_WRONLY, 0o644, dir_fd=fd)
            os.write(wfd, b"hi")
            os.close(wfd)
            assert (tmp_path / "f.txt").read_bytes() == b"hi"
        finally:
            os.close(fd)


class TestDirectoryFdRefusal:
    """The safe-path passthrough must not hand out host directory fds.

    Read-only opens on safe system paths return a real host fd. For a
    directory that fd is a durable capability -- it outlives the safe-path
    check that granted it, and anything reaching a raw syscall can still use
    it. Files keep working; directories do not.
    """

    def test_cannot_open_safe_path_directory(self):
        vfs = VirtualFS({})
        with patch(vfs):
            with pytest.raises(IsADirectoryError) as exc:
                os.open(sys.prefix, os.O_RDONLY)
            assert exc.value.errno == errno.EISDIR

    def test_safe_path_file_reads_still_pass_through(self):
        """The hardening is scoped to directories -- file reads are unaffected."""
        host_file = os.path.join(os.path.dirname(os.__file__), "os.py")
        assert os.path.isfile(host_file)

        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open(host_file, os.O_RDONLY)
            try:
                assert os.read(fd, 5)
            finally:
                os.close(fd)

    def test_listdir_on_safe_path_still_works(self):
        """The virtualized enumeration path keeps its safe-path fallback."""
        vfs = VirtualFS({})
        with patch(vfs):
            assert os.listdir(sys.prefix)

    def test_directory_open_outside_patch_context_unaffected(self, tmp_path):
        fd = os.open(tmp_path, os.O_RDONLY)
        os.close(fd)


class TestContextIsolation:
    """Test that patching is safe across threads using contextvars."""

    def test_thread_pool_context_isolation(self):
        """Test VFS context isolation in thread pool."""
        import contextvars

        vfs_1 = VirtualFS({})
        vfs_2 = VirtualFS({})

        def worker(vfs, content):
            """Worker function that runs in thread pool."""
            with patch(vfs):
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

        with patch(vfs_outer):
            with open("outer.txt", "w") as f:
                f.write("outer")

            with patch(vfs_inner):
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
            with patch(vfs):
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
            with patch(vfs):
                # Opening by file descriptor should still work (bypass patching)
                pass
        finally:
            os.remove(tmp_path)

    def test_islink_patched(self):
        """Test that os.path.islink() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with patch(vfs):
            assert os.path.islink("test.txt") is False
            assert os.path.islink("nonexistent.txt") is False

    def test_lexists_patched(self):
        """Test that os.path.lexists() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with patch(vfs):
            assert os.path.lexists("test.txt") is True
            assert os.path.lexists("nonexistent.txt") is False

    def test_samefile_patched(self):
        """Test that os.path.samefile() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with patch(vfs):
            assert os.path.samefile("test.txt", "test.txt") is True
            assert os.path.samefile("test.txt", "./test.txt") is True

            vfs.write("other.txt", b"other")
            assert os.path.samefile("test.txt", "other.txt") is False

    def test_realpath_patched(self):
        """Test that os.path.realpath() is patched for VFS."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"content")

        with patch(vfs):
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
                self.mkdir_calls = []

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
                return path == "/" or any(f.startswith(path + "/") for f in self._files)

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
                self.mkdir_calls.append((self._resolve(path), parents, exist_ok))

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
        with patch(fs):
            assert os.path.exists("test.txt") is True
            assert os.path.isfile("test.txt") is True
            assert os.listdir("/") == ["test.txt"]
            stat_result = os.stat("test.txt")
            assert stat_result.st_size == 7

    def test_mkdir_does_not_forward_undeclared_mode(self):
        """os.mkdir must not pass mode= to a backend implementing the protocol.

        The FileSystem protocol declares mkdir(path, parents, exist_ok) -- a
        backend written to that signature used to get TypeError because the
        patch layer forwarded an undeclared mode kwarg.
        """
        fs = self._make_minimal_fs()
        with patch(fs):
            os.mkdir("/somedir")
            os.mkdir("/otherdir", 0o700)
        assert fs.mkdir_calls == [
            ("/somedir", False, False),
            ("/otherdir", False, False),
        ]

    def test_pathlib_mkdir_does_not_forward_undeclared_mode(self):
        """pathlib passes mode positionally to os.mkdir; it must stop there."""
        from pathlib import Path

        fs = self._make_minimal_fs()
        with patch(fs):
            Path("/a").mkdir()
            Path("/b").mkdir(mode=0o700, exist_ok=True)
        assert [call[0] for call in fs.mkdir_calls] == ["/a", "/b"]

    def test_rmdir_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="rmdir"):
                os.rmdir("/somedir")

    def test_islink_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="islink"):
                os.path.islink("test.txt")

    def test_samefile_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="samefile"):
                os.path.samefile("test.txt", "test.txt")

    def test_realpath_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="realpath"):
                os.path.realpath("test.txt")

    def test_getsize_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="getsize"):
                os.path.getsize("test.txt")

    def test_replace_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="replace"):
                os.replace("test.txt", "other.txt")

    def test_access_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="access"):
                os.access("test.txt", os.R_OK)

    def test_readlink_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="readlink"):
                os.readlink("test.txt")

    def test_symlink_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="symlink"):
                os.symlink("test.txt", "link.txt")

    def test_link_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="link"):
                os.link("test.txt", "copy.txt")

    def test_chmod_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="chmod"):
                os.chmod("test.txt", 0o755)

    def test_chown_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        if not hasattr(os, "chown"):
            pytest.skip("os.chown not available on this platform")
        with patch(fs):
            with pytest.raises(NotImplementedError, match="chown"):
                os.chown("test.txt", 1000, 1000)

    def test_truncate_raises_not_implemented(self):
        fs = self._make_minimal_fs()
        with patch(fs):
            with pytest.raises(NotImplementedError, match="truncate"):
                os.truncate("test.txt", 0)


class TestOptionalMethods:
    """Test optional methods work through patching with VirtualFS."""

    def test_replace(self):
        fs = VirtualFS({})
        fs.write("a.txt", b"data")

        with patch(fs):
            os.replace("a.txt", "b.txt")

        assert fs.isfile("b.txt")
        assert not fs.isfile("a.txt")

    def test_access(self):
        fs = VirtualFS({})
        fs.write("a.txt", b"data")

        with patch(fs):
            assert os.access("a.txt", os.R_OK) is True
            assert os.access("missing.txt", os.R_OK) is False

    def test_readlink_raises(self):
        fs = VirtualFS({})
        fs.write("a.txt", b"data")

        with patch(fs):
            with pytest.raises(OSError):
                os.readlink("a.txt")

    def test_symlink_raises(self):
        fs = VirtualFS({})

        with patch(fs):
            with pytest.raises(OSError):
                os.symlink("target", "link")

    def test_link(self):
        fs = VirtualFS({})
        fs.write("a.txt", b"data")

        with patch(fs):
            os.link("a.txt", "b.txt")

        assert fs.read("b.txt") == b"data"

    def test_chmod_noop(self):
        fs = VirtualFS({})
        fs.write("a.txt", b"data")

        with patch(fs):
            os.chmod("a.txt", 0o755)  # should not raise

    def test_chmod_missing_file(self):
        fs = VirtualFS({})

        with patch(fs):
            with pytest.raises(FileNotFoundError):
                os.chmod("missing.txt", 0o755)

    def test_chown_noop(self):
        if not hasattr(os, "chown"):
            pytest.skip("os.chown not available on this platform")

        fs = VirtualFS({})
        fs.write("a.txt", b"data")

        with patch(fs):
            os.chown("a.txt", 1000, 1000)  # should not raise

    def test_truncate(self):
        fs = VirtualFS({})
        fs.write("a.txt", b"hello world")

        with patch(fs):
            os.truncate("a.txt", 5)

        assert fs.read("a.txt") == b"hello"

    def test_truncate_missing_file(self):
        fs = VirtualFS({})

        with patch(fs):
            with pytest.raises(FileNotFoundError):
                os.truncate("missing.txt", 0)


class TestIsolatedPatching:
    """Test patching with IsolatedFS."""

    def test_isolated_realpath(self, tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "test.txt").write_text("content")

        isolated = IsolatedFS(str(root))

        with patch(isolated):
            assert os.path.realpath("test.txt") == "/test.txt"
            assert os.path.realpath("./test.txt") == "/test.txt"
            assert os.path.realpath("/test.txt") == "/test.txt"

    def test_isolated_realpath_escape_returns_normalized(self, tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()

        isolated = IsolatedFS(str(root))

        with patch(isolated):
            # Paths that escape the sandbox should return a normalized
            # absolute path rather than "/" so downstream code gets a
            # sensible path that simply won't exist in the VFS.
            assert os.path.realpath("../../etc/passwd") == "/etc/passwd"
            assert os.path.realpath("/../../outside") == "/outside"

    def test_isolated_islink(self, tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        file = root / "test.txt"
        file.write_text("content")

        link = root / "link.txt"
        link.symlink_to(file)

        isolated = IsolatedFS(str(root))

        with patch(isolated):
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

        isolated = IsolatedFS(str(root))

        with patch(isolated):
            assert os.path.samefile("test.txt", "link.txt") is True
            assert os.path.samefile("test.txt", "test.txt") is True


class TestShutilFlagDisabling:
    """Test that shutil optimization flags are disabled during patch()."""

    def test_flags_disabled_inside_context(self):
        """shutil optimization flags should be False inside patch()."""
        import shutil

        vfs = VirtualFS({})

        with patch(vfs):
            if hasattr(shutil, "_use_fd_functions"):
                assert shutil._use_fd_functions is False
            if hasattr(shutil, "_HAS_FCOPYFILE"):
                assert shutil._HAS_FCOPYFILE is False
            if hasattr(shutil, "_USE_CP_SENDFILE"):
                assert shutil._USE_CP_SENDFILE is False

    def test_flags_restored_after_context(self):
        """shutil optimization flags should be restored after patch()."""
        import shutil

        # Capture original values
        originals = {}
        for flag in (
            "_use_fd_functions",
            "_HAS_FCOPYFILE",
            "_USE_CP_SENDFILE",
            "_USE_CP_COPY_FILE_RANGE",
        ):
            if hasattr(shutil, flag):
                originals[flag] = getattr(shutil, flag)

        vfs = VirtualFS({})
        with patch(vfs):
            pass

        # Verify restoration
        for flag, expected in originals.items():
            assert getattr(shutil, flag) == expected

    def test_flags_restored_on_exception(self):
        """shutil flags should be restored even if an exception occurs."""
        import shutil

        originals = {}
        for flag in ("_use_fd_functions", "_HAS_FCOPYFILE", "_USE_CP_SENDFILE"):
            if hasattr(shutil, flag):
                originals[flag] = getattr(shutil, flag)

        vfs = VirtualFS({})
        try:
            with patch(vfs):
                raise RuntimeError("boom")
        except RuntimeError:
            pass

        for flag, expected in originals.items():
            assert getattr(shutil, flag) == expected

    def test_shutil_copyfile_works_in_context(self):
        """shutil.copyfile should work through patched open() inside patch()."""
        import shutil

        vfs = VirtualFS({})
        vfs.write("src.txt", b"copy me")

        with patch(vfs):
            shutil.copyfile("src.txt", "dst.txt")

        assert vfs.read("dst.txt") == b"copy me"

    def test_shutil_copy_works_with_chmod(self):
        """shutil.copy (includes chmod) should work with VirtualFS."""
        import shutil

        fs = VirtualFS({})
        fs.write("src.txt", b"copy me")

        with patch(fs):
            shutil.copy("src.txt", "dst.txt")

        assert fs.read("dst.txt") == b"copy me"

    def test_shutil_rmtree_works_in_context(self):
        """shutil.rmtree should work through patched functions inside patch()."""
        import shutil

        fs = VirtualFS({})
        fs.write("/mydir/a.txt", b"aaa")
        fs.write("/mydir/b.txt", b"bbb")

        with patch(fs):
            shutil.rmtree("mydir")

        assert not fs.isfile("mydir/a.txt")
        assert not fs.isfile("mydir/b.txt")

    def test_shutil_rmtree_removes_nested_directories(self):
        """rmtree must recurse into subdirectories, not just flat ones.

        ``patch()`` forces ``shutil._use_fd_functions = False`` so rmtree
        takes the string-path ``_rmtree_unsafe`` route. That route calls
        ``entry.is_junction()`` on every entry it decides is a directory
        (CPython 3.12+), so a tree with any subdirectory in it reached a
        method ``MockDirEntry`` did not define and raised ``AttributeError``.
        A flat directory never exercises it.
        """
        import shutil

        fs = VirtualFS({})
        fs.write("/tree/top.txt", b"top")
        fs.write("/tree/sub/mid.txt", b"mid")
        fs.write("/tree/sub/deeper/leaf.txt", b"leaf")

        with patch(fs):
            shutil.rmtree("tree")

        assert not fs.isfile("tree/top.txt")
        assert not fs.isfile("tree/sub/mid.txt")
        assert not fs.isfile("tree/sub/deeper/leaf.txt")
        assert not fs.isdir("tree/sub/deeper")
        assert not fs.isdir("tree/sub")
        assert not fs.isdir("tree")

    def test_mock_dir_entry_is_junction_is_false(self):
        """A virtual filesystem has no junctions; entries must say so.

        A junction is a Windows NTFS construct, not a symlink -- the real
        ``os.DirEntry.is_junction()`` returns False on every non-Windows
        platform and for anything that is not a junction.
        """
        import os

        fs = VirtualFS({})
        fs.write("/d/sub/f.txt", b"x")

        with patch(fs):
            entries = {e.name: e for e in os.scandir("/d")}

        assert entries["sub"].is_dir()
        assert entries["sub"].is_junction() is False

    def test_shutil_copy2_works_on_an_isolated_root(self, tmp_path):
        """copy2 copies metadata, so it needs the backend to have utime().

        ``IsolatedFS`` implemented every other optional method the patch layer
        dispatches to, so ``copystat()`` -- and with it ``copy2()`` and
        ``copytree()`` -- died on ``NotImplementedError`` against an isolated
        root.
        """
        import shutil

        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "src.txt").write_text("copy me")
        # Age the source: without this, "now" and "the source's mtime" are the
        # same value and a copy2() that preserved nothing would still pass.
        os.utime(root / "src.txt", (1_600_000_000, 1_600_000_000))

        with patch(IsolatedFS(str(root))):
            shutil.copy2("/src.txt", "/dst.txt")

        assert (root / "dst.txt").read_text() == "copy me"
        assert (root / "dst.txt").stat().st_mtime == pytest.approx(
            1_600_000_000, abs=1e-3
        ), "copy2() did not preserve the source's mtime"

    @pytest.mark.skipif(not hasattr(os, "chflags"), reason="chflags is BSD/macOS only")
    def test_chflags_reports_enotsup(self, tmp_path):
        """BSD file flags have no filesystem to land on -- say so, don't guess.

        Unpatched, ``os.chflags()`` took a virtual path straight to the host.
        ``shutil.copystat()`` calls it for every symlink
        ``copytree(symlinks=True)`` recreates and tolerates exactly ENOTSUP.
        """
        import errno

        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "f.txt").write_text("x")

        with patch(IsolatedFS(str(root))):
            with pytest.raises(OSError) as exc:
                os.chflags("/f.txt", 0)

        assert exc.value.errno == errno.ENOTSUP

    @pytest.mark.skipif(
        not hasattr(os, "listxattr"), reason="extended attributes are Linux only"
    )
    def test_xattr_calls_answer_without_touching_the_host(self, tmp_path):
        """Extended attributes must not resolve against the real filesystem.

        ``shutil.copystat()`` calls ``os.listxattr()`` through ``_copyxattr()``,
        which CPython only defines when ``os.listxattr`` exists -- so on macOS
        the whole path is a no-op and this escape is invisible. On Linux an
        unpatched ``os.listxattr("/src.txt")`` reached the host and raised
        ENOENT, which ``_copyxattr()`` does not tolerate, taking ``copy2()``
        and ``copytree()`` with it.

        Every path used here is absent from the host, so a host syscall would
        raise ``FileNotFoundError`` instead of the answers asserted below.
        """
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "f.txt").write_text("x")

        with patch(IsolatedFS(str(root))):
            assert os.listxattr("/f.txt") == []
            assert os.listxattr("/f.txt", follow_symlinks=False) == []
            # Not even for a path the filesystem does not have: answering from
            # the host would describe some other file entirely.
            assert os.listxattr("/nope.txt") == []

            with pytest.raises(OSError) as get_exc:
                os.getxattr("/f.txt", "user.thing")
            assert get_exc.value.errno == errno.ENODATA

            with pytest.raises(OSError) as set_exc:
                os.setxattr("/f.txt", "user.thing", b"value")
            assert set_exc.value.errno == errno.ENOTSUP

            with pytest.raises(OSError) as rm_exc:
                os.removexattr("/f.txt", "user.thing")
            assert rm_exc.value.errno == errno.ENOTSUP

        # copystat() tolerates ENOTSUP/ENODATA/EINVAL around the listing and
        # EPERM/EACCES in the copy loop -- and nothing else.
        assert get_exc.value.errno in (errno.ENOTSUP, errno.ENODATA, errno.EINVAL)

    def test_xattr_shims_are_installed_and_dispatch_on_the_filesystem(
        self, tmp_path, monkeypatch
    ):
        """The wiring check that has to run on every platform, Linux or not.

        The xattr family only exists on Linux, so a skipif'd test leaves a
        macOS developer blind to the shims being unregistered or dispatching
        wrongly -- which is how the unpatched ``os.listxattr()`` survived in
        the first place. This drives the shims directly and fakes the family
        into ``os`` so the ``install()`` wiring is exercised everywhere.
        """
        import importlib

        from monkeyfs import IsolatedFS
        from monkeyfs.patching import patches
        from monkeyfs.patching.core import _originals

        # monkeyfs.patching re-exports install(), which shadows the submodule
        # of the same name on a plain import.
        install_mod = importlib.import_module("monkeyfs.patching.install")

        root = tmp_path / "root"
        root.mkdir()
        (root / "f.txt").write_text("x")

        host_calls = []

        def recorder(name, result=None):
            def fake(*args, **kwargs):
                host_calls.append((name, args, kwargs))
                return result

            return fake

        monkeypatch.setitem(_originals, "listxattr", recorder("listxattr", ["user.h"]))
        monkeypatch.setitem(_originals, "getxattr", recorder("getxattr", b"host"))
        monkeypatch.setitem(_originals, "setxattr", recorder("setxattr"))
        monkeypatch.setitem(_originals, "removexattr", recorder("removexattr"))

        with patch(IsolatedFS(str(root))):
            assert patches._vfs_listxattr("/f.txt") == []
            for call, expected in (
                (lambda: patches._vfs_getxattr("/f.txt", "user.thing"), errno.ENODATA),
                (
                    lambda: patches._vfs_setxattr("/f.txt", "user.thing", b"v"),
                    errno.ENOTSUP,
                ),
                (
                    lambda: patches._vfs_removexattr("/f.txt", "user.thing"),
                    errno.ENOTSUP,
                ),
            ):
                with pytest.raises(OSError) as exc:
                    call()
                assert exc.value.errno == expected

        assert host_calls == [], "an active filesystem must not reach host xattrs"

        # With no filesystem active the shims are pass-throughs.
        assert patches._vfs_listxattr("/f.txt") == ["user.h"]
        assert patches._vfs_getxattr("/f.txt", "user.thing") == b"host"
        patches._vfs_setxattr("/f.txt", "user.thing", b"v")
        patches._vfs_removexattr("/f.txt", "user.thing")
        assert [name for name, _, _ in host_calls] == [
            "listxattr",
            "getxattr",
            "setxattr",
            "removexattr",
        ]

        # And install() actually registers them. On a platform without the
        # family, fake it in so the registration still gets exercised;
        # monkeypatch drops the attributes again afterwards.
        for name in ("listxattr", "getxattr", "setxattr", "removexattr"):
            monkeypatch.setattr(os, name, recorder(name), raising=False)
        monkeypatch.setattr(install_mod, "_has_xattr", True)
        install_mod._apply_patches()

        assert os.listxattr is patches._vfs_listxattr
        assert os.getxattr is patches._vfs_getxattr
        assert os.setxattr is patches._vfs_setxattr
        assert os.removexattr is patches._vfs_removexattr


class TestPatchReentrancy:
    """Overlapping patch() contexts must not restore process globals early.

    ``tempfile.tempdir`` and shutil's optimization flags are process-global,
    not per-context: shutil and tempfile read them from their own module
    namespace. If an exiting patch() restores them while another context is
    still live, that live context silently regains the fd-based code paths
    that bypass the active filesystem.
    """

    FLAGS = (
        "_use_fd_functions",
        "_HAS_FCOPYFILE",
        "_USE_CP_SENDFILE",
        "_USE_CP_COPY_FILE_RANGE",
    )

    @pytest.fixture(autouse=True)
    def _restore_globals(self):
        """Keep a failing run from leaking corrupted globals into other tests."""
        import shutil
        import tempfile

        saved = {f: getattr(shutil, f) for f in self.FLAGS if hasattr(shutil, f)}
        if hasattr(shutil, "_rmtree_impl"):
            saved["_rmtree_impl"] = shutil._rmtree_impl
        saved_tempdir = tempfile.tempdir
        try:
            yield
        finally:
            for flag, value in saved.items():
                setattr(shutil, flag, value)
            tempfile.tempdir = saved_tempdir

    def _snapshot(self):
        """Current values of every process-global that patch() touches."""
        import shutil
        import tempfile

        snap = {f: getattr(shutil, f) for f in self.FLAGS if hasattr(shutil, f)}
        if hasattr(shutil, "_rmtree_impl"):
            snap["_rmtree_impl"] = shutil._rmtree_impl
        snap["tempdir"] = tempfile.tempdir
        return snap

    def _assert_patched(self, snap):
        """Assert a snapshot taken inside a live patch() shows patched values."""
        import shutil

        for flag in self.FLAGS:
            if flag in snap:
                assert snap[flag] is False, (
                    f"shutil.{flag} was restored while a patch() context was "
                    f"still live -- fd-based code paths bypass the filesystem"
                )
        if "_rmtree_impl" in snap:
            assert snap["_rmtree_impl"] is shutil._rmtree_unsafe, (
                "shutil._rmtree_impl was restored while a patch() context was "
                "still live -- rmtree traverses by fd and bypasses the filesystem"
            )
        assert snap["tempdir"] is None, (
            "tempfile.tempdir was restored while a patch() context was still "
            "live -- temp paths resolve against the host"
        )

    def _run_interleaved(self, inner_body, inner_fs=None):
        """Overlap two patch() contexts across threads, deterministically.

        Ordering, enforced with events (no sleeps): outer enters, inner enters
        while outer is live, outer exits, ``inner_body(fs)`` runs while inner
        is still live, inner exits. Returns whatever ``inner_body`` returned.
        """
        timeout = 10.0
        outer_entered = threading.Event()
        inner_entered = threading.Event()
        outer_exited = threading.Event()
        fs = inner_fs if inner_fs is not None else VirtualFS({})

        def outer():
            try:
                with patch(VirtualFS({})):
                    outer_entered.set()
                    assert inner_entered.wait(timeout), "inner never entered"
            finally:
                outer_entered.set()
                outer_exited.set()

        def inner():
            try:
                assert outer_entered.wait(timeout), "outer never entered"
                with patch(fs):
                    inner_entered.set()
                    assert outer_exited.wait(timeout), "outer never exited"
                    return inner_body(fs)
            finally:
                inner_entered.set()

        with ThreadPoolExecutor(max_workers=2) as pool:
            outer_future = pool.submit(outer)
            inner_future = pool.submit(inner)
            outer_future.result(timeout=timeout * 2)
            return inner_future.result(timeout=timeout * 2)

    def test_overlapping_threads_keep_globals_patched(self):
        """An exiting context must not un-patch globals a live context needs."""
        self._assert_patched(self._run_interleaved(lambda fs: self._snapshot()))

    def test_overlapping_threads_keep_rmtree_on_the_filesystem(self):
        """The observable consequence: rmtree must still route through the FS."""
        import shutil

        fs = VirtualFS({})
        fs.write("/mydir/a.txt", b"aaa")

        def remove_tree(fs):
            shutil.rmtree("/mydir")

        self._run_interleaved(remove_tree, inner_fs=fs)
        assert not fs.isfile("/mydir/a.txt")

    def test_globals_restored_after_overlapping_contexts_exit(self):
        """Interleaved exits must leave the originals, not stale patched values."""
        before = self._snapshot()
        self._run_interleaved(lambda fs: None)
        assert self._snapshot() == before

    def test_overlapping_async_tasks_keep_globals_patched(self):
        """patch() advertises async-safety; overlapping tasks must hold too."""

        async def scenario():
            timeout = 10.0
            outer_entered = asyncio.Event()
            inner_entered = asyncio.Event()
            outer_exited = asyncio.Event()

            async def outer():
                try:
                    with patch(VirtualFS({})):
                        outer_entered.set()
                        await asyncio.wait_for(inner_entered.wait(), timeout)
                finally:
                    outer_entered.set()
                    outer_exited.set()

            async def inner():
                try:
                    await asyncio.wait_for(outer_entered.wait(), timeout)
                    with patch(VirtualFS({})):
                        inner_entered.set()
                        await asyncio.wait_for(outer_exited.wait(), timeout)
                        return self._snapshot()
                finally:
                    inner_entered.set()

            _, snap = await asyncio.gather(outer(), inner())
            return snap

        self._assert_patched(asyncio.run(scenario()))

    def test_entering_a_context_keeps_an_already_resolved_tempdir(self):
        """A later entry must not discard a temp directory already resolved.

        ``tempfile.gettempdir()`` caches its answer back into the
        ``tempfile.tempdir`` slot. Clearing that slot on every entry means a
        later context's resolution outlives its own exit -- ``_exit_globals()``
        returns early while another context is live, so the earlier context is
        left using the later filesystem's temp directory.
        """
        import tempfile

        with patch(VirtualFS({})):
            resolved = tempfile.gettempdir()
            assert tempfile.tempdir == resolved, "gettempdir() should cache"

            with patch(VirtualFS({})):
                pass

            assert tempfile.tempdir == resolved, (
                "entering a nested context cleared the outer context's "
                "already-resolved tempdir; the outer context will now "
                "re-resolve against whichever filesystem is live next"
            )

    def test_overlapping_thread_keeps_an_already_resolved_tempdir(self):
        """Same single-slot hazard, across threads rather than nesting."""
        import tempfile

        with patch(VirtualFS({})):
            resolved = tempfile.gettempdir()
            self._run_interleaved(lambda fs: None)
            assert tempfile.tempdir == resolved, (
                "a concurrent context cleared the live context's resolved "
                f"tempdir ({resolved!r} -> {tempfile.tempdir!r})"
            )

    def test_nested_contexts_restore_only_on_outermost_exit(self):
        """Same-thread nesting: the inner exit must leave the globals patched."""
        before = self._snapshot()
        with patch(VirtualFS({})):
            with patch(VirtualFS({})):
                pass
            self._assert_patched(self._snapshot())
        assert self._snapshot() == before


class TestTransitiveCoverage:
    """Test that stdlib functions work transitively through patched primitives."""

    def test_os_walk(self):
        """os.walk should work through patched os.scandir."""
        vfs = VirtualFS({})
        vfs.write("a/b/c.txt", b"deep")
        vfs.write("a/top.txt", b"top")
        vfs.write("x.txt", b"root")

        with patch(vfs):
            walked = []
            for dirpath, dirnames, filenames in os.walk("/"):
                walked.append((dirpath, sorted(dirnames), sorted(filenames)))

        # Root
        assert walked[0][1] == ["a"]
        assert walked[0][2] == ["x.txt"]

        # Find the 'a' entry and 'a/b' entry
        a_entries = [
            w for w in walked if w[0].rstrip("/").endswith("/a") or w[0] == "a"
        ]
        assert len(a_entries) == 1
        assert "top.txt" in a_entries[0][2]
        assert "b" in a_entries[0][1]

    def test_glob_glob(self):
        """glob.glob should work through patched functions."""
        import glob

        vfs = VirtualFS({})
        vfs.write("data/file1.csv", b"a")
        vfs.write("data/file2.csv", b"b")
        vfs.write("data/readme.txt", b"c")

        with patch(vfs):
            matches = sorted(glob.glob("/data/*.csv"))

        assert len(matches) == 2
        assert any("file1.csv" in m for m in matches)
        assert any("file2.csv" in m for m in matches)

    def test_glob_recursive(self):
        """glob.glob with recursive=True should work."""
        import glob

        vfs = VirtualFS({})
        vfs.write("a/b/deep.txt", b"x")
        vfs.write("a/shallow.txt", b"y")

        with patch(vfs):
            matches = sorted(glob.glob("/a/**/*.txt", recursive=True))

        assert len(matches) == 2

    def test_os_path_getmtime(self):
        """os.path.getmtime should work through patched os.stat."""
        import time

        vfs = VirtualFS({})
        vfs.write("f.txt", b"data")

        with patch(vfs):
            mtime = os.path.getmtime("f.txt")

        assert mtime > 0
        assert mtime <= time.time()

    def test_os_removedirs(self):
        """os.removedirs should work through patched os.rmdir."""
        fs = VirtualFS({})
        fs.mkdir("/a")
        fs.mkdir("/a/b")
        fs.mkdir("/a/b/c")

        with patch(fs):
            os.removedirs("a/b/c")

        assert not fs.isdir("a/b/c")


class TestFcntlPatching:
    """Test that fcntl is patched to no-op under VFS."""

    def test_fcntl_noop(self):
        """fcntl.fcntl should no-op under VFS."""
        try:
            import fcntl
        except ImportError:
            pytest.skip("fcntl not available on this platform")

        vfs = VirtualFS({})
        with patch(vfs):
            result = fcntl.fcntl(0, fcntl.F_GETFL)
            assert result == 0

    def test_flock_noop(self):
        """fcntl.flock should no-op under VFS."""
        try:
            import fcntl
        except ImportError:
            pytest.skip("fcntl not available on this platform")

        vfs = VirtualFS({})
        with patch(vfs):
            # Should not raise
            fcntl.flock(0, fcntl.LOCK_EX)

    def test_lockf_noop(self):
        """fcntl.lockf should no-op under VFS."""
        try:
            import fcntl
        except ImportError:
            pytest.skip("fcntl not available on this platform")

        vfs = VirtualFS({})
        with patch(vfs):
            # Should not raise
            fcntl.lockf(0, fcntl.LOCK_EX)

    def test_fcntl_passthrough_outside_context(self):
        """fcntl should work normally outside VFS context."""
        try:
            import fcntl
        except ImportError:
            pytest.skip("fcntl not available on this platform")

        import tempfile

        # Create a real file to test with
        with tempfile.NamedTemporaryFile() as tmp:
            fd = tmp.fileno()
            # Should call real fcntl, not raise
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            assert isinstance(flags, int)


class TestUtimeArguments:
    """``os.utime()`` is reached almost entirely through ``ns=``.

    ``shutil.copystat()`` -- and with it ``copy2()`` and ``copytree()`` --
    only ever calls ``os.utime(dst, ns=(...), follow_symlinks=...)``. The
    shim accepted ``times`` and swallowed everything else, so the ``ns``
    pair was discarded and the destination was stamped with *now*.
    """

    @staticmethod
    def _isolated_root(tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        return root, IsolatedFS(str(root))

    def test_utime_ns_sets_the_requested_time(self, tmp_path):
        """``ns=`` was dropped entirely, leaving the file stamped with now."""
        root, fs = self._isolated_root(tmp_path)
        (root / "f.txt").write_text("x")
        wanted = 1_600_000_000

        with patch(fs):
            os.utime("/f.txt", ns=(wanted * 10**9, wanted * 10**9))

        assert (root / "f.txt").stat().st_mtime == pytest.approx(wanted, abs=1e-3)

    def test_utime_ns_reaches_a_virtual_backend_too(self):
        """The conversion happens at the patch boundary, not in one backend."""
        vfs = VirtualFS({})
        vfs.write("/f.txt", b"x")
        wanted = 1_600_000_000

        with patch(vfs):
            os.utime("/f.txt", ns=(wanted * 10**9, wanted * 10**9))
            observed = os.path.getmtime("/f.txt")

        assert observed == pytest.approx(wanted, abs=1e-3)

    def test_utime_times_still_works(self, tmp_path):
        """The documented ``times=`` path is unchanged."""
        root, fs = self._isolated_root(tmp_path)
        (root / "f.txt").write_text("x")

        with patch(fs):
            os.utime("/f.txt", (1_600_000_000.0, 1_600_000_000.0))

        assert (root / "f.txt").stat().st_mtime == pytest.approx(
            1_600_000_000, abs=1e-3
        )

    def test_utime_rejects_times_and_ns_together(self, tmp_path):
        """Real ``os.utime()`` raises ValueError; the shim accepted both."""
        root, fs = self._isolated_root(tmp_path)
        (root / "f.txt").write_text("x")

        with patch(fs):
            with pytest.raises(ValueError, match="either 'times' or 'ns'"):
                os.utime("/f.txt", (1, 1), ns=(1, 1))
            # Even an explicit ns=None counts as "specified", as it does in
            # CPython's argument clinic.
            with pytest.raises(ValueError, match="either 'times' or 'ns'"):
                os.utime("/f.txt", (1, 1), ns=None)

    def test_utime_rejects_a_malformed_ns(self, tmp_path):
        """Matching the real TypeErrors, including for an explicit ns=None."""
        root, fs = self._isolated_root(tmp_path)
        (root / "f.txt").write_text("x")

        with patch(fs):
            with pytest.raises(TypeError, match="'ns' must be a tuple of two ints"):
                os.utime("/f.txt", ns=None)
            with pytest.raises(TypeError, match="'ns' must be a tuple of two ints"):
                os.utime("/f.txt", ns=(1,))
            with pytest.raises(TypeError, match="cannot be interpreted as an integer"):
                os.utime("/f.txt", ns=(1.5, 2.5))

    def test_utime_rejects_a_malformed_times(self, tmp_path):
        root, fs = self._isolated_root(tmp_path)
        (root / "f.txt").write_text("x")

        with patch(fs):
            with pytest.raises(TypeError, match="'times' must be either a tuple"):
                os.utime("/f.txt", (1,))


class TestUtimeFollowSymlinks:
    """``follow_symlinks=False`` means "the link, not its target"."""

    @staticmethod
    def _sandbox(tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "target.txt").write_text("target")
        os.utime(root / "target.txt", (1_600_000_000, 1_600_000_000))
        os.symlink("target.txt", root / "link")
        return root, IsolatedFS(str(root))

    def test_utime_on_a_link_refuses_instead_of_retargeting(self, tmp_path):
        """Dropping the flag stamped the *target*, which is what it forbids.

        No backend exposes an ``lutime()``, so the link's own timestamps are
        unreachable. ENOTSUP is the same answer ``_reject_dir_fd()`` gives for
        the same reason.
        """
        root, fs = self._sandbox(tmp_path)
        before = (root / "target.txt").stat().st_mtime

        refused = None
        with patch(fs):
            try:
                os.utime("/link", (1, 1), follow_symlinks=False)
            except OSError as exc:
                refused = exc

        # The damage first: silently dropping the flag stamps the target.
        assert (root / "target.txt").stat().st_mtime == before, (
            "utime(follow_symlinks=False) modified the link's target"
        )
        assert refused is not None, (
            "utime(follow_symlinks=False) on a link should refuse, not no-op"
        )
        assert refused.errno == errno.ENOTSUP

    def test_utime_on_a_link_follows_by_default(self, tmp_path):
        """The default is unchanged: it stamps the target."""
        root, fs = self._sandbox(tmp_path)

        with patch(fs):
            os.utime("/link", (1_700_000_000, 1_700_000_000))

        assert (root / "target.txt").stat().st_mtime == pytest.approx(
            1_700_000_000, abs=1e-3
        )

    def test_follow_symlinks_false_on_a_plain_file_is_allowed(self, tmp_path):
        """Refuse only where it would matter -- a non-link has no target."""
        root, fs = self._sandbox(tmp_path)

        with patch(fs):
            os.utime(
                "/target.txt", (1_700_000_000, 1_700_000_000), follow_symlinks=False
            )

        assert (root / "target.txt").stat().st_mtime == pytest.approx(
            1_700_000_000, abs=1e-3
        )

    def test_copystat_never_reaches_utime_with_follow_symlinks_false(self, tmp_path):
        """The shim is deliberately absent from ``os.supports_follow_symlinks``.

        ``copystat()`` membership-tests the function object it finds in ``os``
        and substitutes a no-op when it is missing, so ``copytree(symlinks=True)``
        does not trip the refusal above.
        """
        import shutil

        root, fs = self._sandbox(tmp_path)
        os.symlink("target.txt", root / "link2")
        before = (root / "target.txt").stat().st_mtime

        with patch(fs):
            assert os.utime not in os.supports_follow_symlinks
            shutil.copystat("/link", "/link2", follow_symlinks=False)

        assert (root / "target.txt").stat().st_mtime == before


class TestCopyTimestampPreservation:
    """``copy2()``/``copytree()`` exist to preserve metadata; assert it."""

    def test_stat_reports_nanosecond_timestamps(self, tmp_path):
        """``copystat()`` reads ``st_*_ns`` and passes it straight to ``utime()``.

        ``os.stat_result`` built from a bare 10-tuple answers ``None`` for all
        three ``_ns`` fields, so the shim was handing ``copystat()`` a source
        timestamp of ``None`` -- honoring ``ns=`` in ``os.utime()`` alone would
        still have preserved nothing.
        """
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "f.txt").write_text("x")
        os.utime(root / "f.txt", (1_600_000_000, 1_600_000_000))

        with patch(IsolatedFS(str(root))):
            st = os.stat("/f.txt")

        assert st.st_mtime_ns == pytest.approx(1_600_000_000 * 10**9, abs=1_000_000)
        assert st.st_atime_ns is not None
        assert st.st_ctime_ns is not None

    def test_copy2_preserves_mtime(self, tmp_path):
        import shutil

        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "src.txt").write_text("copy me")
        old = 1_600_000_000
        os.utime(root / "src.txt", (old, old))

        with patch(IsolatedFS(str(root))):
            shutil.copy2("/src.txt", "/dst.txt")

        assert (root / "dst.txt").stat().st_mtime == pytest.approx(
            (root / "src.txt").stat().st_mtime, abs=1e-3
        ), "copy2() stamped the destination with now instead of the source's mtime"

    def test_copytree_preserves_mtime(self, tmp_path):
        import shutil

        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        (root / "tree").mkdir(parents=True)
        (root / "tree" / "f.txt").write_text("copy me")
        old = 1_600_000_000
        os.utime(root / "tree" / "f.txt", (old, old))

        with patch(IsolatedFS(str(root))):
            shutil.copytree("/tree", "/copy")

        assert (root / "copy" / "f.txt").stat().st_mtime == pytest.approx(
            old, abs=1e-3
        ), "copytree() stamped the copy with now instead of the source's mtime"


class TestChmodFollowSymlinks:
    """``os.chmod(follow_symlinks=False)`` means "the link, not its target"."""

    @staticmethod
    def _sandbox(tmp_path):
        from monkeyfs import IsolatedFS

        root = tmp_path / "root"
        root.mkdir()
        (root / "target.txt").write_text("target")
        (root / "target.txt").chmod(0o644)
        os.symlink("target.txt", root / "link")
        return root, IsolatedFS(str(root))

    def test_chmod_on_a_link_refuses_instead_of_retargeting(self, tmp_path):
        """The same defect ``os.utime()`` had: the flag was dropped in kwargs.

        ``IsolatedFS.chmod()`` resolves the final component, so
        ``os.chmod("/link", mode, follow_symlinks=False)`` -- which means "the
        link, not what it points at" -- rewrote the *target's* permission bits.
        No backend exposes an ``lchmod()``, so ENOTSUP is the answer, exactly
        as for ``os.utime()`` and ``dir_fd``.
        """
        import stat as stat_mod

        root, fs = self._sandbox(tmp_path)

        refused = None
        with patch(fs):
            try:
                os.chmod("/link", 0o600, follow_symlinks=False)
            except OSError as exc:
                refused = exc

        # The damage first: dropping the flag chmods the target.
        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o644, (
            "chmod(follow_symlinks=False) changed the link's target"
        )
        assert refused is not None, (
            "chmod(follow_symlinks=False) on a link should refuse, not no-op"
        )
        assert refused.errno == errno.ENOTSUP

    def test_path_lchmod_refuses_on_a_link(self, tmp_path):
        """``Path.lchmod()`` is the cross-platform way into the same hole.

        ``pathlib.Path.lchmod(mode)`` is defined as
        ``self.chmod(mode, follow_symlinks=False)`` -- it never touches
        ``os.lchmod`` -- so this route reached the target's mode on Linux too,
        where ``os.lchmod`` does not even exist.
        """
        import stat as stat_mod
        from pathlib import Path

        root, fs = self._sandbox(tmp_path)

        refused = None
        with patch(fs):
            try:
                Path("/link").lchmod(0o600)
            except OSError as exc:
                refused = exc

        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o644, (
            "Path.lchmod() changed the link's target"
        )
        assert refused is not None and refused.errno == errno.ENOTSUP

    def test_chmod_on_a_link_follows_by_default(self, tmp_path):
        """The default is unchanged: it chmods the target."""
        import stat as stat_mod

        root, fs = self._sandbox(tmp_path)

        with patch(fs):
            os.chmod("/link", 0o600)

        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o600

    def test_follow_symlinks_false_on_a_plain_file_is_allowed(self, tmp_path):
        """Refuse only where it would matter -- a non-link has no target."""
        import stat as stat_mod

        root, fs = self._sandbox(tmp_path)

        with patch(fs):
            os.chmod("/target.txt", 0o600, follow_symlinks=False)

        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o600

    def test_copystat_never_reaches_chmod_with_follow_symlinks_false(self, tmp_path):
        """The shim stays out of ``os.supports_follow_symlinks`` for this reason.

        ``copystat()`` substitutes a no-op for anything missing from that set,
        so ``copytree(symlinks=True)`` does not trip the refusal above.
        """
        import shutil
        import stat as stat_mod

        root, fs = self._sandbox(tmp_path)
        os.symlink("target.txt", root / "link2")

        with patch(fs):
            assert os.chmod not in os.supports_follow_symlinks
            shutil.copystat("/link", "/link2", follow_symlinks=False)

        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o644


class TestBsdLinkShims:
    """``os.lchmod()`` and ``os.lchflags()`` exist on BSD/macOS only.

    Neither was patched, and until the CI matrix grew a macOS job no run
    anywhere could execute a test for them: on Linux the attributes do not
    exist, so a virtual path reaching the host went unnoticed on the only
    platform that was ever tested.
    """

    @staticmethod
    def _sandbox(tmp_path):
        from monkeyfs import IsolatedFS

        outside = tmp_path / "outside.txt"
        outside.write_text("a real file, outside the sandbox")
        outside.chmod(0o644)

        root = tmp_path / "root"
        root.mkdir()
        (root / "target.txt").write_text("target")
        (root / "target.txt").chmod(0o644)
        os.symlink("target.txt", root / "link")
        return outside, root, IsolatedFS(str(root))

    @pytest.mark.skipif(not hasattr(os, "lchmod"), reason="lchmod is BSD/macOS only")
    def test_lchmod_does_not_reach_the_host(self, tmp_path):
        """Unpatched, ``os.lchmod()`` chmod'ed a real file from inside a sandbox."""
        import stat as stat_mod

        outside, _root, fs = self._sandbox(tmp_path)

        refused = None
        with patch(fs):
            try:
                os.lchmod(str(outside), 0o600)
            except OSError as exc:
                refused = exc

        # The escape first: this is a file outside the sandbox entirely.
        assert stat_mod.S_IMODE(outside.lstat().st_mode) == 0o644, (
            "os.lchmod() reached the host filesystem and changed the mode of a "
            "real file outside the sandbox"
        )
        assert refused is not None, "os.lchmod() under patch() must not succeed"
        # It is answered from inside the sandbox, where an absolute host path
        # names nothing -- the same answer os.chmod() already gave for it.
        assert refused.errno == errno.ENOENT

    @pytest.mark.skipif(not hasattr(os, "lchmod"), reason="lchmod is BSD/macOS only")
    def test_lchmod_on_a_link_refuses_rather_than_retargeting(self, tmp_path):
        """A sandbox-internal link is the other half: never chmod the target.

        ``os.lchmod(path, mode)`` is ``os.chmod(path, mode,
        follow_symlinks=False)``, so it lands on the same refusal.
        """
        import stat as stat_mod

        _outside, root, fs = self._sandbox(tmp_path)

        with patch(fs):
            with pytest.raises(OSError) as exc:
                os.lchmod("/link", 0o600)

        assert exc.value.errno == errno.ENOTSUP
        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o644, (
            "os.lchmod() on a link changed the link's target"
        )

    @pytest.mark.skipif(not hasattr(os, "lchmod"), reason="lchmod is BSD/macOS only")
    def test_lchmod_on_a_plain_file_still_chmods_it(self, tmp_path):
        """The refusal is narrow: a non-link has no target to protect."""
        import stat as stat_mod

        _outside, root, fs = self._sandbox(tmp_path)

        with patch(fs):
            os.lchmod("/target.txt", 0o600)

        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o600

    @pytest.mark.skipif(not hasattr(os, "lchmod"), reason="lchmod is BSD/macOS only")
    def test_shutil_copymode_between_two_links_never_reaches_the_host(self, tmp_path):
        """``copymode()`` is the one stdlib route to ``os.lchmod()``.

        With ``follow_symlinks=False`` and both paths symlinks, CPython 3.10
        through 3.14 all call ``os.lchmod(dst, mode)`` with no exception
        handling of any kind -- so whatever it does propagates straight out of
        ``shutil.copy(src, dst, follow_symlinks=False)``. Unpatched, that call
        carried the virtual path ``/link2`` to the host.
        """
        import shutil
        import stat as stat_mod

        _outside, root, fs = self._sandbox(tmp_path)
        (root / "other.txt").write_text("other")
        (root / "other.txt").chmod(0o600)
        os.symlink("other.txt", root / "link2")

        with patch(fs):
            with pytest.raises(OSError) as exc:
                shutil.copymode("/link", "/link2", follow_symlinks=False)

        assert exc.value.errno == errno.ENOTSUP
        # Neither link's target may have been touched.
        assert stat_mod.S_IMODE((root / "target.txt").lstat().st_mode) == 0o644
        assert stat_mod.S_IMODE((root / "other.txt").lstat().st_mode) == 0o600

    @pytest.mark.skipif(
        not hasattr(os, "lchflags"), reason="lchflags is BSD/macOS only"
    )
    def test_lchflags_does_not_reach_the_host(self, tmp_path):
        """Same escape as ``os.chflags()``, on the link-only sibling.

        BSD file flags are not part of the ``FileSystem`` protocol and no
        backend carries them, so there is nothing to set -- and unpatched,
        ``os.lchflags()`` set them on a real file instead.
        """
        import stat as stat_mod

        outside, _root, fs = self._sandbox(tmp_path)
        assert outside.lstat().st_flags == 0

        refused = None
        with patch(fs):
            try:
                os.lchflags(str(outside), stat_mod.UF_NODUMP)
            except OSError as exc:
                refused = exc

        assert outside.lstat().st_flags == 0, (
            "os.lchflags() reached the host filesystem and set flags on a real "
            "file outside the sandbox"
        )
        assert refused is not None, "os.lchflags() under patch() must not succeed"
        assert refused.errno == errno.ENOTSUP

    def test_link_shims_are_installed_and_dispatch_on_the_filesystem(
        self, tmp_path, monkeypatch
    ):
        """The wiring check that has to run on every platform, BSD or not.

        ``os.lchmod`` and ``os.lchflags`` exist only on BSD/macOS, so a
        skipif'd test leaves a Linux contributor blind to the shims being
        unregistered or dispatching wrongly -- which is exactly how both went
        unpatched. This drives them directly and fakes them into ``os`` so the
        ``install()`` wiring is exercised everywhere.
        """
        import importlib

        from monkeyfs.patching import patches
        from monkeyfs.patching.core import _originals

        # monkeyfs.patching re-exports install(), which shadows the submodule
        # of the same name on a plain import.
        install_mod = importlib.import_module("monkeyfs.patching.install")

        _outside, _root, fs = self._sandbox(tmp_path)

        host_calls = []

        def recorder(name):
            def fake(*args, **kwargs):
                host_calls.append((name, args, kwargs))

            return fake

        monkeypatch.setitem(_originals, "lchmod", recorder("lchmod"))
        monkeypatch.setitem(_originals, "lchflags", recorder("lchflags"))

        with patch(fs):
            for call in (
                lambda: patches._vfs_lchmod("/link", 0o600),
                lambda: patches._vfs_lchflags("/link", 0),
                lambda: patches._vfs_lchflags("/target.txt", 0),
            ):
                with pytest.raises(OSError) as exc:
                    call()
                assert exc.value.errno == errno.ENOTSUP

        assert host_calls == [], (
            "an active filesystem must not reach the host lchmod/lchflags"
        )

        # With no filesystem active the shims are pass-throughs.
        patches._vfs_lchmod("/link", 0o600)
        patches._vfs_lchflags("/link", 0)
        assert [name for name, _, _ in host_calls] == ["lchmod", "lchflags"]

        # And install() actually registers them. On a platform without the
        # pair, fake them in so the registration still gets exercised.
        had = {name: getattr(os, name, None) for name in ("lchmod", "lchflags")}
        try:
            os.lchmod = recorder("lchmod")  # type: ignore[assignment]
            os.lchflags = recorder("lchflags")  # type: ignore[assignment]
            install_mod._apply_patches()

            assert os.lchmod is patches._vfs_lchmod
            assert os.lchflags is patches._vfs_lchflags
        finally:
            # Put back what was there -- on BSD that is the shim install()
            # applied for real, and dropping it would leave the rest of the
            # session unpatched.
            for name, value in had.items():
                if value is None:
                    delattr(os, name)
                else:
                    setattr(os, name, value)
