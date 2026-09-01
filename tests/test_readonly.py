"""Tests for ReadOnlyFS."""

import glob as glob_mod
import os
from pathlib import Path

import pytest

from monkeyfs import IsolatedFS, MountFS, VirtualFS, patch
from monkeyfs.readonly import ReadOnlyFS


def _make_ro():
    """Create a ReadOnlyFS wrapping a VirtualFS with some files."""
    vfs = VirtualFS({})
    vfs.write("file.txt", b"hello")
    vfs.write("data/report.csv", b"a,b,c")
    vfs.mkdir("empty_dir")
    return ReadOnlyFS(vfs)


class TestReadOnlyFSReads:
    """Read operations should delegate transparently."""

    def test_read_file(self):
        ro = _make_ro()
        assert ro.read("file.txt") == b"hello"

    def test_open_read_text(self):
        ro = _make_ro()
        f = ro.open("file.txt", "r")
        assert f.read() == "hello"

    def test_open_read_binary(self):
        ro = _make_ro()
        f = ro.open("file.txt", "rb")
        assert f.read() == b"hello"

    def test_exists(self):
        ro = _make_ro()
        assert ro.exists("file.txt")
        assert not ro.exists("nope.txt")

    def test_isfile(self):
        ro = _make_ro()
        assert ro.isfile("file.txt")
        assert not ro.isfile("data")

    def test_isdir(self):
        ro = _make_ro()
        assert ro.isdir("data")
        assert not ro.isdir("file.txt")

    def test_list(self):
        ro = _make_ro()
        entries = ro.list("/")
        assert "file.txt" in entries
        assert "data" in entries

    def test_list_recursive(self):
        ro = _make_ro()
        entries = ro.list("/", recursive=True)
        assert "data/report.csv" in entries

    def test_stat(self):
        ro = _make_ro()
        meta = ro.stat("file.txt")
        assert meta.size == 5
        assert not meta.is_dir

    def test_getcwd(self):
        ro = _make_ro()
        assert ro.getcwd() == "/"

    def test_chdir(self):
        ro = _make_ro()
        ro.chdir("data")
        assert ro.getcwd() == "/data"

    def test_glob(self):
        ro = _make_ro()
        matches = ro.glob("*.txt")
        assert "file.txt" in matches


class TestReadOnlyFSBlocks:
    """Write operations should raise PermissionError."""

    def test_open_write(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("file.txt", "w")

    def test_open_append(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("file.txt", "a")

    def test_open_exclusive(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("new.txt", "x")

    def test_open_readwrite(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.open("file.txt", "r+")

    def test_write(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.write("file.txt", b"new")

    def test_write_many(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.write_many({"a.txt": b"a"})

    def test_remove(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.remove("file.txt")

    def test_remove_many(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.remove_many(["file.txt"])

    def test_mkdir(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.mkdir("newdir")

    def test_makedirs(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.makedirs("a/b/c")

    def test_rename(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.rename("file.txt", "other.txt")

    def test_rmdir(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.rmdir("empty_dir")

    def test_replace(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.replace("file.txt", "other.txt")

    def test_truncate(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.truncate("file.txt", 0)

    def test_symlink(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.symlink("file.txt", "link.txt")

    def test_link(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.link("file.txt", "link.txt")

    def test_chmod(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.chmod("file.txt", 0o644)

    def test_chown(self):
        ro = _make_ro()
        with pytest.raises(PermissionError, match="Read-only"):
            ro.chown("file.txt", 0, 0)


class TestReadOnlyFSAccess:
    """access() should deny write, allow read."""

    def test_write_denied(self):
        ro = _make_ro()
        assert not ro.access("file.txt", os.W_OK)

    def test_read_allowed(self):
        ro = _make_ro()
        assert ro.access("file.txt", os.R_OK)

    def test_exists_check(self):
        ro = _make_ro()
        assert ro.access("file.txt", os.F_OK)


class TestReadOnlyFSWithPatch:
    """ReadOnlyFS should work through the patch() context."""

    def test_patched_open_read(self):
        ro = _make_ro()
        with patch(ro):
            with open("file.txt", "r") as f:
                assert f.read() == "hello"

    def test_patched_open_write_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                open("file.txt", "w")

    def test_patched_os_remove_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                os.remove("file.txt")

    def test_patched_os_mkdir_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                os.mkdir("newdir")

    def test_patched_os_rename_blocked(self):
        ro = _make_ro()
        with patch(ro):
            with pytest.raises(PermissionError):
                os.rename("file.txt", "other.txt")


class TestReadOnlyFSComposition:
    """ReadOnlyFS should compose with different filesystem types."""

    def test_wraps_virtualfs(self):
        vfs = VirtualFS({})
        vfs.write("test.txt", b"data")
        ro = ReadOnlyFS(vfs)
        assert ro.read("test.txt") == b"data"
        with pytest.raises(PermissionError):
            ro.write("test.txt", b"new")

    def test_inner_fs_still_writable(self):
        """Wrapping doesn't affect the inner filesystem."""
        vfs = VirtualFS({})
        vfs.write("test.txt", b"data")
        ro = ReadOnlyFS(vfs)
        # Inner FS is still writable
        vfs.write("test.txt", b"updated")
        # ReadOnlyFS sees the update
        assert ro.read("test.txt") == b"updated"


def _attempt(fn):
    """Run ``fn`` and return the exception it raised, or ``None``.

    Lets a test assert on the *filesystem's* state first and on the refusal
    second. ``pytest.raises`` fails on "DID NOT RAISE" before any state check
    runs, so a leak reads as a missing exception rather than as the mutation it
    actually is.
    """
    try:
        fn()
    except BaseException as exc:  # noqa: BLE001 -- the exception is the result
        return exc
    return None


class TestReadOnlyFSUtime:
    """Timestamps are file state, and ReadOnlyFS must not let them be rewritten.

    Every test here asserts the *wrapped* filesystem's stored mtime is
    unchanged before it asserts anything about an exception -- a test that
    checks only "it raised" cannot tell a refusal from a successful write.
    """

    OLD = 1_000_000_000.0  # 2001-09-09
    NEW = 2_000_000_000.0  # 2033-05-18

    def _aged_vfs(self):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")
        vfs.utime("file.txt", (self.OLD, self.OLD))
        return vfs, vfs.stat("file.txt").modified_at

    def _assert_intact(self, vfs, before, exc, op):
        after = vfs.stat("file.txt").modified_at
        assert after == before, (
            f"{op} rewrote the wrapped filesystem's mtime through ReadOnlyFS: "
            f"{before} -> {after} (call raised: {exc!r})"
        )
        assert isinstance(exc, PermissionError), (
            f"{op} should be refused with PermissionError, got {exc!r}"
        )

    def test_direct_utime_refused_and_mtime_intact(self):
        vfs, before = self._aged_vfs()
        ro = ReadOnlyFS(vfs)
        exc = _attempt(lambda: ro.utime("file.txt", (self.NEW, self.NEW)))
        self._assert_intact(vfs, before, exc, "ReadOnlyFS.utime()")

    def test_patched_os_utime_refused_and_mtime_intact(self):
        vfs, before = self._aged_vfs()
        with patch(ReadOnlyFS(vfs)):
            exc = _attempt(lambda: os.utime("file.txt", (self.NEW, self.NEW)))
        self._assert_intact(vfs, before, exc, "os.utime(times=)")

    def test_patched_os_utime_ns_refused_and_mtime_intact(self):
        """``shutil.copystat()`` reaches os.utime() through ``ns=``, not ``times=``."""
        vfs, before = self._aged_vfs()
        ns = int(self.NEW * 1_000_000_000)
        with patch(ReadOnlyFS(vfs)):
            exc = _attempt(lambda: os.utime("file.txt", ns=(ns, ns)))
        self._assert_intact(vfs, before, exc, "os.utime(ns=)")

    def test_patched_path_touch_refused_and_mtime_intact(self):
        """``Path.touch()`` on an existing file is a utime, not a create."""
        vfs, before = self._aged_vfs()
        with patch(ReadOnlyFS(vfs)):
            exc = _attempt(Path("file.txt").touch)
        self._assert_intact(vfs, before, exc, "Path.touch()")

    def test_patched_copystat_refused_and_mtime_intact(self):
        """``shutil.copystat()`` stamped the destination before chmod refused."""
        import shutil

        vfs = VirtualFS({})
        vfs.write("src.txt", b"source")
        vfs.write("dst.txt", b"dest")
        vfs.utime("dst.txt", (self.OLD, self.OLD))
        vfs.utime("src.txt", (self.NEW, self.NEW))
        before = vfs.stat("dst.txt").modified_at

        with patch(ReadOnlyFS(vfs)):
            exc = _attempt(lambda: shutil.copystat("src.txt", "dst.txt"))

        after = vfs.stat("dst.txt").modified_at
        assert after == before, (
            f"shutil.copystat() rewrote the destination mtime through "
            f"ReadOnlyFS: {before} -> {after} (call raised: {exc!r})"
        )

    def test_isolated_utime_refused_and_mtime_intact(self, tmp_path):
        """IsolatedFS only recently gained ``utime()``; until then the hole was
        masked by NotImplementedError rather than closed by the wrapper."""
        iso = IsolatedFS(str(tmp_path))
        iso.write("file.txt", b"hello")
        os.utime(tmp_path / "file.txt", (self.OLD, self.OLD))
        before = os.stat(tmp_path / "file.txt").st_mtime

        with patch(ReadOnlyFS(iso)):
            exc = _attempt(lambda: os.utime("file.txt", (self.NEW, self.NEW)))

        after = os.stat(tmp_path / "file.txt").st_mtime
        assert after == before, (
            f"os.utime() rewrote the real file's mtime through a ReadOnlyFS "
            f"over IsolatedFS: {before} -> {after} (call raised: {exc!r})"
        )
        assert isinstance(exc, PermissionError), (
            f"os.utime() should be refused with PermissionError, got {exc!r}"
        )


# ---------------------------------------------------------------------------
# Systematic coverage of the mutating surface
# ---------------------------------------------------------------------------


class _Spy:
    """Delegates every attribute to a real filesystem, recording each fetch.

    Wrapped *inside* a ReadOnlyFS, so anything recorded here is something the
    wrapper let through.
    """

    def __init__(self, inner):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "reached", [])

    def __getattr__(self, name):
        inner = object.__getattribute__(self, "_inner")
        attr = getattr(inner, name)  # AttributeError propagates, as it should
        if not callable(attr):
            return attr
        reached = object.__getattribute__(self, "reached")

        def recording(*args, **kwargs):
            reached.append(name)
            return attr(*args, **kwargs)

        return recording


# Every method of the FileSystem interface that changes stored state. Held here
# rather than imported from monkeyfs.readonly so the test is an independent
# enumeration that the implementation has to agree with, not an echo of it.
MUTATING_METHODS = {
    "chmod": ("file.txt", 0o600),
    "chown": ("file.txt", 0, 0),
    "link": ("file.txt", "hard.txt"),
    "makedirs": ("a/b/c",),
    "mkdir": ("newdir",),
    "mount": ("/mnt", None),
    "remove": ("file.txt",),
    "remove_many": (["file.txt"],),
    "rename": ("file.txt", "other.txt"),
    "replace": ("file.txt", "other.txt"),
    "rmdir": ("empty_dir",),
    "symlink": ("file.txt", "link.txt"),
    "truncate": ("file.txt", 0),
    "unmount": ("/mnt",),
    "utime": ("file.txt", (1_000_000_000.0, 1_000_000_000.0)),
    "write": ("file.txt", b"new"),
    "write_many": ({"a.txt": b"a"},),
}

# Every method of the FileSystem interface that only observes. ``chdir`` moves
# the filesystem's own cwd and stores nothing, so it counts as a read here --
# the same call it has always been allowed to make.
READING_METHODS = {
    "access",
    "chdir",
    "exists",
    "get_metadata_snapshot",
    "getcwd",
    "getsize",
    "glob",
    "invalidate",
    "isdir",
    "isfile",
    "islink",
    "lexists",
    "list",
    "list_detailed",
    "open",
    "read",
    "readlink",
    "realpath",
    "resolve_path",
    "samefile",
    "stat",
}


def _public_methods(cls):
    return {
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name, None))
    }


class TestMutatingSurfaceIsRefused:
    """The regression guard: nothing that mutates may reach the wrapped fs.

    A denylist widens silently every time a backend gains a method. These
    three tests are what makes that impossible to do quietly.
    """

    @pytest.mark.parametrize("method", sorted(MUTATING_METHODS))
    def test_mutating_method_is_refused(self, method):
        args = MUTATING_METHODS[method]
        base = VirtualFS({})
        base.write("file.txt", b"hello")
        base.mkdir("empty_dir")
        # mount()/unmount() belong to MountFS; everything else to VirtualFS,
        # which implements the whole optional surface.
        backend = MountFS(base) if method in ("mount", "unmount") else base
        spy = _Spy(backend)
        ro = ReadOnlyFS(spy)

        exc = _attempt(lambda: getattr(ro, method)(*args))

        assert method not in spy.reached, (
            f"ReadOnlyFS forwarded the mutating call {method}() to the wrapped "
            f"filesystem (raised afterwards: {exc!r})"
        )
        assert isinstance(exc, PermissionError), (
            f"ReadOnlyFS.{method}() should be refused with PermissionError, got {exc!r}"
        )

    @pytest.mark.parametrize(
        "backend", [VirtualFS, IsolatedFS, MountFS], ids=lambda c: c.__name__
    )
    def test_every_backend_method_is_classified(self, backend):
        """A backend gaining a method must fail here until it is classified.

        ``IsolatedFS.utime()`` was added without anyone revisiting ReadOnlyFS,
        which is how a wrapper's guarantee gets widened by a change to
        something it wraps.
        """
        unclassified = (
            _public_methods(backend) - MUTATING_METHODS.keys() - READING_METHODS
        )
        assert not unclassified, (
            f"{backend.__name__} has method(s) {sorted(unclassified)} that this "
            f"test does not classify. Decide whether each one reads or mutates, "
            f"then add it to READING_METHODS or MUTATING_METHODS here and to the "
            f"matching set in monkeyfs/readonly.py."
        )

    @pytest.mark.parametrize("method", sorted(READING_METHODS))
    def test_reading_method_still_delegates(self, method):
        """The inverse failure: an allowlist too tight to pass reads through."""
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")
        ro = ReadOnlyFS(vfs)
        assert hasattr(VirtualFS, method), (
            f"VirtualFS no longer implements {method}(); update READING_METHODS"
        )
        exc = _attempt(lambda: getattr(ro, method))
        assert exc is None or isinstance(exc, AttributeError), (
            f"ReadOnlyFS refused the read-only method {method}(): {exc!r}"
        )


class TestPatchLayerMutatorsRefused:
    """Every mutating entry point the patch layer dispatches, end to end."""

    @staticmethod
    def _fixture():
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")
        vfs.write("dir/inner.txt", b"inner")
        vfs.mkdir("empty_dir")
        spy = _Spy(vfs)
        return spy, ReadOnlyFS(spy)

    OPS = {
        "builtins.open(a)": lambda: open("file.txt", "a"),
        "builtins.open(r+)": lambda: open("file.txt", "r+"),
        "builtins.open(w)": lambda: open("file.txt", "w"),
        "builtins.open(x)": lambda: open("new.txt", "x"),
        "os.chmod": lambda: os.chmod("file.txt", 0o600),
        "os.makedirs": lambda: os.makedirs("a/b/c"),
        "os.mkdir": lambda: os.mkdir("newdir"),
        "os.open(O_CREAT)": lambda: os.open("new.txt", os.O_CREAT | os.O_WRONLY),
        "os.remove": lambda: os.remove("file.txt"),
        "os.rename": lambda: os.rename("file.txt", "other.txt"),
        "os.replace": lambda: os.replace("file.txt", "other.txt"),
        "os.rmdir": lambda: os.rmdir("empty_dir"),
        "os.symlink": lambda: os.symlink("file.txt", "link.txt"),
        "os.truncate": lambda: os.truncate("file.txt", 0),
        "os.unlink": lambda: os.unlink("file.txt"),
        "os.utime": lambda: os.utime("file.txt", (1e9, 1e9)),
        "os.utime(ns)": lambda: os.utime("file.txt", ns=(10**18, 10**18)),
        "pathlib.Path.mkdir": lambda: Path("newdir").mkdir(),
        "pathlib.Path.touch(existing)": lambda: Path("file.txt").touch(),
        "pathlib.Path.touch(new)": lambda: Path("new.txt").touch(),
        "pathlib.Path.unlink": lambda: Path("file.txt").unlink(),
        **(
            {"os.chown": lambda: os.chown("file.txt", os.getuid(), os.getgid())}
            if hasattr(os, "chown")
            else {}
        ),
        **(
            {"os.link": lambda: os.link("file.txt", "hard.txt")}
            if hasattr(os, "link")
            else {}
        ),
        **(
            {"os.lchmod": lambda: os.lchmod("file.txt", 0o600)}
            if hasattr(os, "lchmod")
            else {}
        ),
    }

    @pytest.mark.parametrize("op", sorted(OPS))
    def test_op_is_refused(self, op):
        spy, ro = self._fixture()
        with patch(ro):
            exc = _attempt(self.OPS[op])
        leaked = sorted(set(spy.reached) & MUTATING_METHODS.keys())
        assert not leaked, (
            f"{op} reached the wrapped filesystem through ReadOnlyFS via "
            f"{leaked} (raised afterwards: {exc!r})"
        )
        assert isinstance(exc, PermissionError), (
            f"{op} should be refused with PermissionError, got {exc!r}"
        )

    @pytest.mark.parametrize("flags", ["O_WRONLY", "O_RDWR"])
    def test_write_mode_fd_never_reaches_the_filesystem(self, flags):
        """A write-mode ``os.open()`` on an existing file is refused at close.

        The fd table buffers in memory and only calls ``fs.write()`` when the
        fd is closed, so the refusal lands there rather than at ``os.open()``.
        Late, but the content never reaches the wrapped filesystem, which is
        the property this asserts.
        """
        spy, ro = self._fixture()
        with patch(ro):
            fd = os.open("file.txt", getattr(os, flags))
            os.write(fd, b"pwned")
            exc = _attempt(lambda: os.close(fd))
        assert "write" not in spy.reached, (
            f"os.open(..., {flags}) carried a write to the wrapped filesystem "
            f"(close raised: {exc!r})"
        )
        assert isinstance(exc, PermissionError), (
            f"closing a write-mode fd should be refused, got {exc!r}"
        )
        assert ro.read("file.txt") == b"hello"

    def test_os_write_on_a_read_fd_is_refused(self):
        spy, ro = self._fixture()
        with patch(ro):
            fd = os.open("file.txt", os.O_RDONLY)
            try:
                exc = _attempt(lambda: os.write(fd, b"pwned"))
            finally:
                os.close(fd)
        assert "write" not in spy.reached, (
            f"os.write() on a read fd reached the wrapped filesystem "
            f"(raised afterwards: {exc!r})"
        )
        assert isinstance(exc, (PermissionError, OSError)), (
            f"os.write() on a read fd should fail, got {exc!r}"
        )


class TestPatchLayerRefusesBeforeReadOnlyFS:
    """Operations the patch layer already refuses, so they never were the
    wrapper's problem. Recorded so a later relaxation there shows up here."""

    @staticmethod
    def _ops():
        ops = {}
        if hasattr(os, "chflags"):
            ops["os.chflags"] = lambda: os.chflags("file.txt", 0)
        if hasattr(os, "lchflags"):
            ops["os.lchflags"] = lambda: os.lchflags("file.txt", 0)
        if hasattr(os, "setxattr"):
            ops["os.setxattr"] = lambda: os.setxattr("file.txt", "user.k", b"v")
        if hasattr(os, "removexattr"):
            ops["os.removexattr"] = lambda: os.removexattr("file.txt", "user.k")
        return ops

    def test_refused_without_touching_the_filesystem(self):
        ops = self._ops()
        if not ops:
            pytest.skip("no BSD-flag or xattr functions on this platform")
        for name, fn in ops.items():
            vfs = VirtualFS({})
            vfs.write("file.txt", b"hello")
            spy = _Spy(vfs)
            with patch(ReadOnlyFS(spy)):
                exc = _attempt(fn)
            leaked = sorted(set(spy.reached) & MUTATING_METHODS.keys())
            assert not leaked, f"{name} reached the wrapped filesystem via {leaked}"
            assert isinstance(exc, OSError), f"{name} should refuse, got {exc!r}"


class TestUnknownAttributesFailClosed:
    """__getattr__ must refuse what it has not classified."""

    def test_unknown_backend_method_is_refused(self):
        class Backend(VirtualFS):
            def frobnicate(self, path):  # pragma: no cover -- must not run
                raise AssertionError("ReadOnlyFS forwarded an unclassified method")

        ro = ReadOnlyFS(Backend({}))
        with pytest.raises(PermissionError) as excinfo:
            ro.frobnicate("file.txt")
        message = str(excinfo.value)
        assert "frobnicate" in message, f"error does not name the method: {message}"

    def test_refusal_explains_how_to_classify(self):
        class Backend(VirtualFS):
            def frobnicate(self, path):  # pragma: no cover -- must not run
                raise AssertionError("ReadOnlyFS forwarded an unclassified method")

        ro = ReadOnlyFS(Backend({}))
        with pytest.raises(PermissionError) as excinfo:
            ro.frobnicate("file.txt")
        message = str(excinfo.value)
        assert "monkeyfs.readonly" in message, (
            f"error does not say where to classify the method: {message}"
        )

    def test_non_callable_attribute_is_refused(self):
        """Handing out a backend's internal state defeats the wrapper entirely."""
        ro = ReadOnlyFS(IsolatedFS(os.path.realpath(os.curdir)))
        with pytest.raises(PermissionError):
            ro.root

    def test_wrapped_filesystem_still_reachable_explicitly(self):
        vfs = VirtualFS({})
        ro = ReadOnlyFS(vfs)
        assert ro._fs is vfs

    def test_missing_optional_method_still_raises_attribute_error(self):
        """``getattr(fs, 'islink', None)`` in the patch layer must keep working."""

        class Bare:
            def exists(self, path):
                return False

        ro = ReadOnlyFS(Bare())
        assert getattr(ro, "islink", None) is None

    def test_dunder_lookup_raises_attribute_error(self):
        """Interpreter machinery probes dunders and expects AttributeError."""
        ro = ReadOnlyFS(VirtualFS({}))
        with pytest.raises(AttributeError):
            ro.__wrapped__

    def test_copy_and_pickle_survive(self):
        """A passthrough that forwards ``_fs`` recurses forever here.

        ``copy`` and ``pickle`` build the new instance without calling
        ``__init__``, so the first ``self._fs`` lookup lands in ``__getattr__``,
        which looks up ``self._fs``.
        """
        import copy
        import pickle

        ro = ReadOnlyFS(VirtualFS({}))
        assert isinstance(copy.copy(ro), ReadOnlyFS)
        assert isinstance(copy.deepcopy(ro), ReadOnlyFS)
        assert isinstance(pickle.loads(pickle.dumps(ro))._fs, VirtualFS)


class TestReadsThroughPatchedOS:
    """Ordinary reads must keep working through the wrapper.

    A too-tight allowlist fails here rather than downstream.
    """

    @staticmethod
    def _ro():
        vfs = VirtualFS({})
        vfs.write("/file.txt", b"hello")
        vfs.write("/data/report.csv", b"a,b,c")
        vfs.mkdir("/empty_dir")
        return ReadOnlyFS(vfs)

    def test_open_for_reading(self):
        with patch(self._ro()):
            with open("/file.txt") as f:
                assert f.read() == "hello"
            with open("/file.txt", "rb") as f:
                assert f.read() == b"hello"

    def test_listdir(self):
        with patch(self._ro()):
            assert set(os.listdir("/")) >= {"file.txt", "data", "empty_dir"}

    def test_scandir(self):
        with patch(self._ro()):
            names = {e.name: e.is_dir() for e in os.scandir("/")}
        assert names["file.txt"] is False
        assert names["data"] is True

    def test_stat(self):
        with patch(self._ro()):
            assert os.stat("/file.txt").st_size == 5
            assert os.path.getsize("/file.txt") == 5
            assert os.path.exists("/file.txt")
            assert os.path.isfile("/file.txt")
            assert os.path.isdir("/data")

    def test_walk(self):
        with patch(self._ro()):
            seen = {root: sorted(files) for root, _dirs, files in os.walk("/")}
        assert "file.txt" in seen["/"]
        assert seen["/data"] == ["report.csv"]

    def test_glob(self):
        with patch(self._ro()):
            assert glob_mod.glob("/*.txt") == ["/file.txt"]
            assert glob_mod.glob("/data/*.csv") == ["/data/report.csv"]

    def test_pathlib_reads(self):
        with patch(self._ro()):
            assert Path("/file.txt").read_text() == "hello"
            assert Path("/file.txt").exists()
            assert sorted(p.name for p in Path("/data").iterdir()) == ["report.csv"]
