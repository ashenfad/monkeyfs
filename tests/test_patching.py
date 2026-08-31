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
