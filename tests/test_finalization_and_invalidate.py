"""Tests for VirtualFile finalization flush and VirtualFS.invalidate()."""

import gc
import warnings

import pytest

from monkeyfs import VirtualFS


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
