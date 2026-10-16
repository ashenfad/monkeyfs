"""A VirtualFS's backing state lives on the host.

A state that keeps its values in real files (a disk-backed store) reads,
writes, deletes and lists them with Python's own ``open`` and ``os``
functions. Under ``patch()`` those are routed into the active filesystem,
so the VirtualFS must reach its state with interception suspended, or the
state's host paths are looked up in the VirtualFS itself.
"""

import os
import threading
from collections.abc import MutableMapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from monkeyfs import VirtualFS, current_fs, patch


class FileBackedState(MutableMapping):
    """A state keeping each value in its own file under ``root``.

    Every operation goes through builtin ``open`` and ``os``, as a disk
    store's does. ``seen`` records the filesystem that was active during
    each call, which must always be None. A call that reaches the state
    again from inside one of its own calls (its I/O routed back through
    the VirtualFS) fails at once rather than recursing without end.
    """

    def __init__(self, root: Path):
        self.root = str(root)
        self.seen: list[object] = []
        self._local = threading.local()

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key.encode().hex())

    @contextmanager
    def _note(self):
        self.seen.append(current_fs.get())
        if getattr(self._local, "busy", False):
            raise AssertionError("the state's own I/O reached the VirtualFS")
        self._local.busy = True
        try:
            yield
        finally:
            self._local.busy = False

    def _load(self, key: str):
        try:
            with open(self._path(key), "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            raise KeyError(key) from None
        # The working directory is stored as str, everything else as bytes.
        return raw[1:].decode() if raw[:1] == b"s" else raw[1:]

    def __getitem__(self, key: str):
        with self._note():
            return self._load(key)

    def __setitem__(self, key: str, value) -> None:
        with self._note():
            tag = b"s" if isinstance(value, str) else b"b"
            raw = tag + (value.encode() if isinstance(value, str) else value)
            with open(self._path(key), "wb") as f:
                f.write(raw)

    def __delitem__(self, key: str) -> None:
        with self._note():
            try:
                os.remove(self._path(key))
            except FileNotFoundError:
                raise KeyError(key) from None

    def __contains__(self, key: object) -> bool:
        with self._note():
            return isinstance(key, str) and os.path.exists(self._path(key))

    def __iter__(self):
        with self._note():
            names = os.listdir(self.root)
        return iter([bytes.fromhex(name).decode() for name in names])

    def __len__(self) -> int:
        with self._note():
            return len(os.listdir(self.root))

    def get_many(self, *keys: str) -> dict:
        with self._note():
            found = {}
            for key in keys:
                try:
                    found[key] = self._load(key)
                except KeyError:
                    pass
            return found


@pytest.fixture
def host(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    state = FileBackedState(store)
    return state, VirtualFS(state)


def test_a_file_written_outside_reads_inside(host):
    state, vfs = host
    payload = os.urandom(64 * 1024)
    vfs.write("/data/a.bin", payload)
    vfs.invalidate()

    with patch(vfs):
        with open("/data/a.bin", "rb") as f:
            assert f.read() == payload
        assert Path("/data/a.bin").read_bytes() == payload
        assert os.path.getsize("/data/a.bin") == len(payload)

    assert state.seen and all(fs is None for fs in state.seen)


def test_a_file_written_inside_reads_outside(host):
    state, vfs = host
    with patch(vfs):
        os.makedirs("/data", exist_ok=True)
        with open("/data/a.txt", "w") as f:
            f.write("hello")
        Path("/data/b.txt").write_text("world")
        with open("/data/a.txt", "a") as f:
            f.write("!")

    assert vfs.read("/data/a.txt") == b"hello!"
    assert vfs.read("/data/b.txt") == b"world"
    assert all(fs is None for fs in state.seen)


def test_listing_inside(host):
    state, vfs = host
    vfs.write("/d/one.txt", b"1")
    vfs.write("/d/sub/two.txt", b"2")
    vfs.invalidate()

    with patch(vfs):
        assert sorted(os.listdir("/d")) == ["one.txt", "sub"]
        assert sorted(e.name for e in os.scandir("/d")) == ["one.txt", "sub"]
        walked = [
            os.path.join(root, name)
            for root, _, files in os.walk("/d")
            for name in files
        ]
        assert sorted(walked) == ["/d/one.txt", "/d/sub/two.txt"]
        assert os.path.isdir("/d/sub")
        assert os.path.isfile("/d/one.txt")
        assert os.path.exists("/d/sub/two.txt")
        assert not os.path.exists("/d/three.txt")
        assert os.stat("/d/one.txt").st_size == 1

    assert all(fs is None for fs in state.seen)


def test_delete_and_rename_inside(host):
    state, vfs = host
    vfs.write("/d/one.txt", b"1")
    vfs.write("/d/two.txt", b"2")
    vfs.write("/t/x/y.txt", b"y")
    vfs.invalidate()

    with patch(vfs):
        os.remove("/d/one.txt")
        os.rename("/d/two.txt", "/d/2.txt")
        os.rename("/t", "/u")
        os.mkdir("/e")
        os.rmdir("/e")
        os.chdir("/d")
        assert os.getcwd() == "/d"
        assert os.listdir(".") == ["2.txt"]

    vfs.invalidate()
    assert vfs.list("/d") == ["2.txt"]
    assert vfs.read("/d/2.txt") == b"2"
    assert vfs.read("/u/x/y.txt") == b"y"
    assert not vfs.exists("/t")
    assert not vfs.exists("/e")
    assert all(fs is None for fs in state.seen)


def test_bulk_reads_inside(host):
    state, vfs = host
    vfs.write_many({"/a.txt": b"a", "/b.txt": b"b"})
    vfs.invalidate()

    with patch(vfs):
        assert vfs.read_many(["/a.txt", "/b.txt"]) == {
            "/a.txt": b"a",
            "/b.txt": b"b",
        }
        assert sorted(vfs.get_metadata_snapshot()) == ["a.txt", "b.txt"]

    assert all(fs is None for fs in state.seen)


def test_suspension_does_not_leak_to_the_caller_or_other_threads(host):
    """Interception is off only for the state call, and only in the
    context making it."""
    state, vfs = host
    vfs.write("/a.txt", b"a")
    ready = threading.Event()
    release = threading.Event()
    observed: list[object] = []

    class Blocking(FileBackedState):
        def __getitem__(self, key):
            ready.set()
            release.wait(5)
            return super().__getitem__(key)

    slow_state = Blocking(Path(state.root))
    slow = VirtualFS(slow_state)

    def other_thread():
        ready.wait(5)
        with patch(vfs):
            observed.append(current_fs.get())
            observed.append(open("/a.txt", "rb").read())
        release.set()

    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(other_thread)
        with patch(slow):
            assert open("/a.txt", "rb").read() == b"a"
            assert current_fs.get() is slow
        future.result(5)

    assert observed == [vfs, b"a"]
