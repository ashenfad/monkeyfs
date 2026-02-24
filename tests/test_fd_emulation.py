"""Tests for low-level fd emulation (os.open, os.read, os.write, etc.)."""

import os
import sys
import tempfile
import threading

import pytest

from monkeyfs import VirtualFS, patch


class TestRawFDOperations:
    def test_os_open_creates_file(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"hello")
            os.close(fd)
        assert vfs.read("/test.txt") == b"hello"

    def test_os_open_excl_raises_on_existing(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"exists")
        with patch(vfs):
            with pytest.raises(FileExistsError):
                os.open("/test.txt", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)

    def test_os_open_without_creat_raises_on_missing(self):
        vfs = VirtualFS({})
        with patch(vfs):
            with pytest.raises(FileNotFoundError):
                os.open("/nonexistent.txt", os.O_RDONLY)

    def test_os_read_write_roundtrip(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"data")
            os.lseek(fd, 0, os.SEEK_SET)
            assert os.read(fd, 100) == b"data"
            os.close(fd)

    def test_os_read_existing_file(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"existing content")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDONLY)
            data = os.read(fd, 100)
            assert data == b"existing content"
            os.close(fd)

    def test_os_open_trunc(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"old content")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_TRUNC, 0o600)
            os.write(fd, b"new")
            os.close(fd)
        assert vfs.read("/test.txt") == b"new"

    def test_os_open_append(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"start")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_WRONLY | os.O_APPEND)
            os.write(fd, b"end")
            os.close(fd)
        assert vfs.read("/test.txt") == b"startend"

    def test_os_lseek(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"abcdef")
            os.lseek(fd, 2, os.SEEK_SET)
            assert os.read(fd, 3) == b"cde"
            os.close(fd)

    def test_os_fstat(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"12345")
            st = os.fstat(fd)
            assert st.st_size == 5
            os.close(fd)

    def test_os_close_flushes_to_vfs(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"flushed")
            os.close(fd)
        assert vfs.read("/test.txt") == b"flushed"

    def test_os_close_rdonly_does_not_overwrite(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"original")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDONLY)
            os.read(fd, 100)
            os.close(fd)
        assert vfs.read("/test.txt") == b"original"

    def test_creates_parent_dirs(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/a/b/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"deep")
            os.close(fd)
        assert vfs.read("/a/b/test.txt") == b"deep"

    def test_write_then_unlink(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"temp")
            os.close(fd)
            os.unlink("/test.txt")
        assert not vfs.exists("/test.txt")


class TestFdopen:
    def test_fdopen_binary_write(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(b"via fdopen")
        assert vfs.read("/test.txt") == b"via fdopen"

    def test_fdopen_text_write(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write("text via fdopen")
        assert vfs.read("/test.txt") == b"text via fdopen"

    def test_fdopen_binary_read(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"read me")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDONLY)
            with os.fdopen(fd, "rb") as f:
                assert f.read() == b"read me"

    def test_fdopen_text_read(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"read text")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDONLY)
            with os.fdopen(fd, "r") as f:
                assert f.read() == "read text"


class TestTempfileIntegration:
    def test_mkstemp(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd, path = tempfile.mkstemp()
            os.write(fd, b"temp data")
            os.close(fd)
            # Verify file exists in VFS
            normalized = path.lstrip("/")
            assert vfs.exists(path) or vfs.exists(normalized)

    def test_mkstemp_write_read_cycle(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd, path = tempfile.mkstemp()
            os.write(fd, b"cycle test")
            os.close(fd)
            # Read it back through VFS
            normalized = path.lstrip("/")
            content = vfs.read(path) if vfs.exists(path) else vfs.read(normalized)
            assert content == b"cycle test"

    def test_mkstemp_unlink(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd, path = tempfile.mkstemp()
            os.write(fd, b"delete me")
            os.close(fd)
            os.unlink(path)
            normalized = path.lstrip("/")
            assert not vfs.exists(path) and not vfs.exists(normalized)

    def test_named_temporary_file(self):
        vfs = VirtualFS({})
        with patch(vfs):
            with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as f:
                f.write(b"ntf data")
                name = f.name
            normalized = name.lstrip("/")
            assert vfs.exists(name) or vfs.exists(normalized)

    def test_mkstemp_with_dir(self):
        vfs = VirtualFS({})
        vfs.makedirs("/mytemp")
        with patch(vfs):
            fd, path = tempfile.mkstemp(dir="/mytemp")
            os.write(fd, b"custom dir")
            os.close(fd)
            assert path.startswith("/mytemp")


class TestEdgeCases:
    def test_read_empty_returns_empty_bytes(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/empty.txt", os.O_RDWR | os.O_CREAT, 0o600)
            assert os.read(fd, 100) == b""
            os.close(fd)

    def test_write_to_rdonly_fd_raises(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"data")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDONLY)
            with pytest.raises(OSError):
                os.write(fd, b"nope")
            os.close(fd)

    def test_read_from_wronly_fd_raises(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"data")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_WRONLY)
            with pytest.raises(OSError):
                os.read(fd, 100)
            os.close(fd)

    def test_multiple_fds_open_simultaneously(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd1 = os.open("/a.txt", os.O_RDWR | os.O_CREAT, 0o600)
            fd2 = os.open("/b.txt", os.O_RDWR | os.O_CREAT, 0o600)
            fd3 = os.open("/c.txt", os.O_RDWR | os.O_CREAT, 0o600)
            assert fd1 != fd2 != fd3
            os.write(fd1, b"aaa")
            os.write(fd2, b"bbb")
            os.write(fd3, b"ccc")
            os.close(fd1)
            os.close(fd2)
            os.close(fd3)
        assert vfs.read("/a.txt") == b"aaa"
        assert vfs.read("/b.txt") == b"bbb"
        assert vfs.read("/c.txt") == b"ccc"

    def test_reopen_after_close(self):
        """Write, close, then reopen and read back."""
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"persisted")
            os.close(fd)
            # Reopen and read
            fd2 = os.open("/test.txt", os.O_RDONLY)
            assert os.read(fd2, 100) == b"persisted"
            os.close(fd2)

    def test_double_close_raises(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.close(fd)
            with pytest.raises(OSError):
                os.close(fd)

    def test_fstat_reflects_buffer_not_persisted(self):
        """fstat size should reflect current buffer, not last-persisted size."""
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"short")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR)
            # Buffer has "short" (5 bytes), write more without closing
            os.lseek(fd, 0, os.SEEK_END)
            os.write(fd, b" and longer")
            st = os.fstat(fd)
            assert st.st_size == len(b"short and longer")
            os.close(fd)

    def test_lseek_seek_end(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"abcdef")
            pos = os.lseek(fd, -2, os.SEEK_END)
            assert pos == 4
            assert os.read(fd, 10) == b"ef"
            os.close(fd)

    def test_lseek_seek_cur(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR | os.O_CREAT, 0o600)
            os.write(fd, b"abcdef")
            os.lseek(fd, 0, os.SEEK_SET)
            os.read(fd, 2)  # position at 2
            pos = os.lseek(fd, 1, os.SEEK_CUR)
            assert pos == 3
            assert os.read(fd, 1) == b"d"
            os.close(fd)


class TestFdopenModes:
    def test_fdopen_read_write_binary(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"original")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR)
            with os.fdopen(fd, "r+b") as f:
                assert f.read() == b"original"
                f.seek(0)
                f.write(b"replaced")
        assert vfs.read("/test.txt") == b"replaced"

    def test_fdopen_read_write_text(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"original")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_RDWR)
            with os.fdopen(fd, "r+") as f:
                assert f.read() == "original"
                f.seek(0)
                f.write("replaced")
        assert vfs.read("/test.txt") == b"replaced"

    def test_fdopen_append_binary(self):
        vfs = VirtualFS({})
        vfs.write("/test.txt", b"start")
        with patch(vfs):
            fd = os.open("/test.txt", os.O_WRONLY | os.O_APPEND)
            with os.fdopen(fd, "ab") as f:
                f.write(b"_end")
        assert vfs.read("/test.txt") == b"start_end"


class TestTempfileExtended:
    def test_named_temporary_file_content_roundtrip(self):
        """NamedTemporaryFile write then read back via VFS."""
        vfs = VirtualFS({})
        with patch(vfs):
            with tempfile.NamedTemporaryFile(mode="w+b", delete=False) as f:
                f.write(b"roundtrip data")
                name = f.name
            content = vfs.read(name)
            assert content == b"roundtrip data"

    @pytest.mark.skipif(
        sys.version_info < (3, 12),
        reason="_TemporaryFileCloser.cleanup with cached unlink added in 3.12",
    )
    def test_named_temporary_file_delete_true(self):
        """NamedTemporaryFile(delete=True) cleans up on close."""
        vfs = VirtualFS({})
        with patch(vfs):
            with tempfile.NamedTemporaryFile(mode="w+b", delete=True) as f:
                f.write(b"auto delete")
                name = f.name
            # After exiting context, file should be deleted
            assert not vfs.exists(name)

    def test_mkdtemp(self):
        vfs = VirtualFS({})
        with patch(vfs):
            dirpath = tempfile.mkdtemp()
            assert vfs.isdir(dirpath)

    def test_mkdtemp_with_prefix(self):
        vfs = VirtualFS({})
        with patch(vfs):
            dirpath = tempfile.mkdtemp(prefix="myprefix_")
            assert "myprefix_" in os.path.basename(dirpath)
            assert vfs.isdir(dirpath)

    def test_temporary_directory(self):
        vfs = VirtualFS({})
        with patch(vfs):
            with tempfile.TemporaryDirectory() as tmpdir:
                assert vfs.isdir(tmpdir)
                # Create a file inside
                fd = os.open(
                    os.path.join(tmpdir, "inner.txt"),
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                os.write(fd, b"inside tmpdir")
                os.close(fd)
            # After exiting, directory should be cleaned up
            assert not vfs.exists(tmpdir)

    def test_spooled_temporary_file(self):
        vfs = VirtualFS({})
        with patch(vfs):
            with tempfile.SpooledTemporaryFile(max_size=1024, mode="w+b") as f:
                f.write(b"spooled data")
                f.seek(0)
                assert f.read() == b"spooled data"

    def test_mkstemp_suffix_and_prefix(self):
        vfs = VirtualFS({})
        with patch(vfs):
            fd, path = tempfile.mkstemp(suffix=".log", prefix="app_")
            basename = os.path.basename(path)
            assert basename.startswith("app_")
            assert basename.endswith(".log")
            os.write(fd, b"log entry")
            os.close(fd)

    def test_mkstemp_multiple_files(self):
        """Multiple mkstemp calls produce unique paths and fds."""
        vfs = VirtualFS({})
        with patch(vfs):
            results = []
            for i in range(5):
                fd, path = tempfile.mkstemp()
                os.write(fd, f"file {i}".encode())
                os.close(fd)
                results.append(path)
            # All paths should be unique
            assert len(set(results)) == 5


class TestThreadSafety:
    def test_concurrent_fd_allocation(self):
        """Multiple threads allocating fds simultaneously."""
        vfs = VirtualFS({})
        fds = []
        errors = []

        def worker(idx):
            try:
                with patch(vfs):
                    fd = os.open(f"/t{idx}.txt", os.O_RDWR | os.O_CREAT, 0o600)
                    os.write(fd, f"thread {idx}".encode())
                    os.close(fd)
                    fds.append(fd)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in threads: {errors}"
        assert len(set(fds)) == 10  # All fds unique
        for i in range(10):
            assert vfs.read(f"/t{i}.txt") == f"thread {i}".encode()
