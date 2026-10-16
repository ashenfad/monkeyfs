"""Tests for VirtualFile finalization flush and VirtualFS.invalidate()."""

import errno
import gc
import subprocess
import sys
import textwrap
import warnings

import pytest

from monkeyfs import LostWriteError, VirtualFS


class _QuotaFS(VirtualFS):
    """Refuses any write over 4 bytes, as a size quota would."""

    def write(self, path, data, *args, **kwargs):
        if len(data) > 4:
            raise OSError(errno.ENOSPC, "quota exceeded", path)
        return super().write(path, data, *args, **kwargs)


class _BrokenFS(VirtualFS):
    """Fails every write with an exception that is not an OSError."""

    def write(self, path, data, *args, **kwargs):
        raise RuntimeError("backend unavailable")


@pytest.fixture
def unraisable(monkeypatch):
    """What reaches ``sys.unraisablehook``, as ``(exc_value, object)``."""
    seen = []
    monkeypatch.setattr(
        sys, "unraisablehook", lambda u: seen.append((u.exc_value, u.object))
    )
    return seen


class TestVirtualFileFinalization:
    """Unclosed write handles must not silently lose data."""

    def test_del_persists_unclosed_write(self):
        vfs = VirtualFS({})

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            vfs.open("out.txt", "w").write("data")
            gc.collect()

        assert vfs.read("out.txt") == b"data"

    def test_del_persists_unclosed_binary_append(self):
        vfs = VirtualFS({})
        vfs.write("log.bin", b"one")

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            vfs.open("log.bin", "ab").write(b"two")
            gc.collect()

        assert vfs.read("log.bin") == b"onetwo"

    def test_del_warns_resource_warning(self):
        vfs = VirtualFS({})

        with pytest.warns(ResourceWarning, match="unclosed file"):
            vfs.open("warned.txt", "w").write("x")
            gc.collect()

    def test_explicit_close_does_not_warn(self):
        vfs = VirtualFS({})

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            f = vfs.open("clean.txt", "w")
            f.write("x")
            f.close()
            del f
            gc.collect()

        assert vfs.read("clean.txt") == b"x"

    def test_context_manager_does_not_warn(self):
        vfs = VirtualFS({})

        with warnings.catch_warnings():
            warnings.simplefilter("error", ResourceWarning)
            with vfs.open("ctx.txt", "w") as f:
                f.write("x")
            gc.collect()

        assert vfs.read("ctx.txt") == b"x"


class TestLostImplicitClose:
    """A save that fails at finalization reaches ``sys.unraisablehook``."""

    def test_failed_save_is_reported_as_lost_write(self, unraisable):
        vfs = _QuotaFS({})

        vfs.open("/big.txt", "w").write("too large")
        gc.collect()

        assert not vfs.exists("/big.txt")
        [(exc, _)] = unraisable
        assert isinstance(exc, LostWriteError)
        assert isinstance(exc, OSError)
        assert exc.filename == "/big.txt"
        assert exc.errno == errno.ENOSPC
        assert "quota exceeded" in str(exc)
        assert isinstance(exc.__cause__, OSError)
        assert exc.__cause__.strerror == "quota exceeded"

    def test_cause_without_errno_reports_eio(self, unraisable):
        vfs = _BrokenFS({})

        vfs.open("/f.bin", "wb").write(b"x")
        gc.collect()

        [(exc, _)] = unraisable
        assert isinstance(exc, LostWriteError)
        assert exc.errno == errno.EIO
        assert "RuntimeError: backend unavailable" in str(exc)
        assert isinstance(exc.__cause__, RuntimeError)

    def test_successful_save_reports_nothing(self, unraisable):
        vfs = _QuotaFS({})

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", ResourceWarning)
            vfs.open("/ok.txt", "w").write("ok")
            gc.collect()

        assert unraisable == []
        assert vfs.read("/ok.txt") == b"ok"

    def test_failed_explicit_close_is_not_reported_again(self, unraisable):
        vfs = _QuotaFS({})

        with pytest.raises(OSError, match="quota exceeded"):
            with vfs.open("/big.txt", "w") as f:
                f.write("too large")
        del f
        gc.collect()

        assert unraisable == []

    @pytest.mark.parametrize(
        "change",
        [
            lambda f: f.write(" and more"),
            lambda f: f.writelines([" and", " more"]),
            lambda f: f.truncate(6),
        ],
        ids=["write", "writelines", "truncate"],
    )
    def test_a_change_after_a_failed_close_is_reported_if_lost(
        self, unraisable, change
    ):
        vfs = _QuotaFS({})
        f = vfs.open("/big.txt", "w")
        f.write("too large")
        with pytest.raises(OSError, match="quota exceeded"):
            f.close()
        change(f)  # content the failed close never reported
        del f
        gc.collect()

        [(exc, _)] = unraisable
        assert isinstance(exc, LostWriteError)
        assert exc.filename == "/big.txt"

    def test_a_failed_encode_on_explicit_close_is_not_reported_again(self, unraisable):
        vfs = VirtualFS({})
        f = vfs.open("/odd.txt", "w")
        f.write("\ud800")  # a lone surrogate: not encodable as UTF-8
        with pytest.raises(UnicodeEncodeError):
            f.close()
        del f
        gc.collect()

        assert unraisable == []

    def test_quiet_while_the_interpreter_shuts_down(self, tmp_path):
        # A file still referenced from a module global is finalized while
        # the interpreter shuts down, when sys.is_finalizing() is true.
        script = tmp_path / "shutdown.py"
        script.write_text(
            textwrap.dedent(
                """
                import errno
                from monkeyfs import VirtualFS

                class QuotaFS(VirtualFS):
                    def write(self, path, data, *args, **kwargs):
                        raise OSError(errno.ENOSPC, "quota exceeded", path)

                held = QuotaFS({}).open("/big.txt", "w")
                held.write("too large")
                """
            )
        )
        done = subprocess.run(
            [sys.executable, "-W", "ignore::ResourceWarning", str(script)],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0
        assert done.stderr == ""

    def test_reported_while_the_interpreter_runs(self, tmp_path):
        # The same file finalized before shutdown is reported, so the quiet
        # shutdown above is the finalizing check and not a lost report.
        script = tmp_path / "running.py"
        script.write_text(
            textwrap.dedent(
                """
                import errno
                from monkeyfs import VirtualFS

                class QuotaFS(VirtualFS):
                    def write(self, path, data, *args, **kwargs):
                        raise OSError(errno.ENOSPC, "quota exceeded", path)

                held = QuotaFS({}).open("/big.txt", "w")
                held.write("too large")
                del held
                """
            )
        )
        done = subprocess.run(
            [sys.executable, "-W", "ignore::ResourceWarning", str(script)],
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0
        assert "LostWriteError" in done.stderr
        assert "/big.txt" in done.stderr


class TestVirtualFSInvalidate:
    """invalidate() drops lazy caches after external state mutation."""

    def test_invalidate_refreshes_listing_and_stat(self):
        state: dict[str, bytes] = {}
        vfs = VirtualFS(state)
        vfs.write("a/keep.txt", b"keep")
        vfs.write("a/drop.txt", b"drop")
        assert sorted(vfs.list("a")) == ["drop.txt", "keep.txt"]

        # Simulate a versioned store rolled back underneath the instance:
        # snapshot state, mutate through the fs, then restore the snapshot.
        snapshot = dict(state)
        vfs.write("a/extra.txt", b"extra")
        vfs.remove("a/drop.txt")
        state.clear()
        state.update(snapshot)

        vfs.invalidate()
        assert sorted(vfs.list("a")) == ["drop.txt", "keep.txt"]
        assert vfs.exists("a/drop.txt")
        assert not vfs.exists("a/extra.txt")
        assert vfs.stat("a/drop.txt").size == 4

    def test_invalidate_recomputes_size_accounting(self):
        state: dict[str, bytes] = {}
        vfs = VirtualFS(state, max_size_mb=1)
        vfs.write("big.bin", b"x" * (1024 * 1024 - 10))

        snapshot = dict(state)
        vfs.remove("big.bin")
        state.clear()
        state.update(snapshot)
        vfs.invalidate()

        # big.bin counts against the limit again after invalidate
        with pytest.raises(OSError):
            vfs.write("more.bin", b"y" * 100)

    def test_invalidate_on_fresh_instance_is_noop(self):
        vfs = VirtualFS({})
        vfs.invalidate()
        assert vfs.list("/") == []
