"""The FileSystem protocol is the single source of truth.

Three things used to be maintained separately and cross-checked by reading
them side by side: the names the patch layer dispatches to, the read/write
classification ``ReadOnlyFS`` enforces, and the set of methods ``MountFS``
forwards. Each drifted at least once -- ``IsolatedFS.utime()`` reached a
backend through a read-only wrapper, and ``MountFS`` quietly dropped
``resolve_path`` and ``readlink`` off a composed filesystem. These tests
assert the three still agree.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS, VirtualFS
from monkeyfs.base import (
    DIRECT_READ_METHODS,
    DIRECT_WRITE_METHODS,
    DISPATCHED_METHODS,
    FORWARDED_METHODS,
    KEY_SCHEME_METHODS,
    OPTIONAL_METHODS,
    OPTIONAL_READ_METHODS,
    OPTIONAL_WRITE_METHODS,
    READ_METHODS,
    REQUIRED_METHODS,
    WRITE_METHODS,
    FileSystem,
)

PATCHING_DIR = pathlib.Path(__file__).resolve().parent.parent / "monkeyfs" / "patching"

# The names the patch layer binds a filesystem to. ``fs`` is the parameter
# every shim takes; ``self.fs`` / ``self._fs`` is how the fd table and the
# scandir entries hold on to one.
_FS_NAMES = {"fs", "_fs"}

# Callers that take the method name as a string argument.
_PROBES = {"_require", "getattr", "hasattr"}


def _dispatched_names() -> dict[str, set[str]]:
    """Every filesystem method the patch layer reaches for, by module."""
    found: dict[str, set[str]] = {}
    for source in sorted(PATCHING_DIR.glob("*.py")):
        names: set[str] = set()
        tree = ast.parse(source.read_text())

        for node in ast.walk(tree):
            # fs.method(...) and self.fs.method(...)
            if isinstance(node, ast.Attribute) and _is_fs_expr(node.value):
                names.add(node.attr)
            # _require(fs, "method"), getattr(fs, "method", ...), hasattr(...)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _PROBES
                and len(node.args) >= 2
                and _is_fs_expr(node.args[0])
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)

        if names:
            found[source.name] = names
    return found


def _is_fs_expr(node: ast.expr) -> bool:
    """True if ``node`` evaluates to the active filesystem."""
    if isinstance(node, ast.Name):
        return node.id in _FS_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in _FS_NAMES
    return False


def _public_methods(cls) -> set[str]:
    return {
        name
        for name in dir(cls)
        if not name.startswith("_") and callable(getattr(cls, name, None))
    }


class TestDispatchSurface:
    """What the patch layer calls must be what the protocol declares."""

    def test_patch_layer_dispatches_only_declared_methods(self):
        undeclared = {
            module: sorted(names - DISPATCHED_METHODS)
            for module, names in _dispatched_names().items()
            if names - DISPATCHED_METHODS
        }
        assert not undeclared, (
            f"the patch layer reaches for filesystem method(s) the protocol "
            f"does not declare: {undeclared}. Add each to REQUIRED_* (called "
            f"unconditionally) or OPTIONAL_* (probed) in monkeyfs/base.py, and "
            f"to the table in docs/api.md."
        )

    def test_every_declared_method_is_actually_dispatched(self):
        """The other direction: a name nobody calls is not part of the surface.

        ``os.path.lexists()`` routes through the ``exists`` shim, so no
        backend needs a ``lexists()`` for the patch layer -- it belongs with
        the direct-use methods, not the dispatched ones.
        """
        unreachable = sorted(
            DISPATCHED_METHODS - set().union(*_dispatched_names().values())
        )
        assert not unreachable, (
            f"the protocol declares {unreachable} as dispatched, but no shim "
            f"in monkeyfs/patching reaches for them. Move each to "
            f"DIRECT_READ_METHODS / DIRECT_WRITE_METHODS, or delete it."
        )

    def test_scan_finds_the_known_dispatch_sites(self):
        """The scan itself has to work, or the test above passes vacuously."""
        names = set().union(*_dispatched_names().values())
        # A required call, an optional one reached through _require(), and one
        # reached through hasattr().
        assert {"stat", "utime", "resolve_path"} <= names

    def test_required_methods_are_the_protocol_body(self):
        """``isinstance(fs, FileSystem)`` must check exactly the required set.

        The optional methods are deliberately absent from the class body: a
        runtime_checkable Protocol checks every method it declares, so adding
        one there would make a conforming backend fail the check for a method
        it is allowed not to have.
        """
        declared = {
            name
            for name, value in vars(FileSystem).items()
            if not name.startswith("_") and callable(value)
        }
        assert declared == REQUIRED_METHODS

    def test_classification_partitions_the_surface(self):
        """Every declared name is classified read or write, never both."""
        assert not (READ_METHODS & WRITE_METHODS)
        assert DISPATCHED_METHODS <= READ_METHODS | WRITE_METHODS
        assert not (REQUIRED_METHODS & OPTIONAL_METHODS)
        assert FORWARDED_METHODS == (
            OPTIONAL_METHODS | DIRECT_READ_METHODS | DIRECT_WRITE_METHODS
        )


class TestVirtualFSSurface:
    """VirtualFS implements the whole protocol, so it is the reference."""

    def test_implements_every_declared_method(self):
        missing = sorted(
            (DISPATCHED_METHODS | DIRECT_READ_METHODS | DIRECT_WRITE_METHODS)
            - _public_methods(VirtualFS)
        )
        assert not missing, f"VirtualFS is missing declared method(s): {missing}"

    def test_isinstance_check_passes(self, tmp_path):
        assert isinstance(VirtualFS({}), FileSystem)
        assert isinstance(IsolatedFS(root=str(tmp_path)), FileSystem)


class TestMountFSForwarding:
    """A composed filesystem must not be narrower than what it wraps."""

    def test_forwards_every_optional_method_the_backend_has(self):
        vfs = VirtualFS({})
        mount = MountFS(vfs)
        missing = sorted(
            name
            for name in FORWARDED_METHODS
            if hasattr(vfs, name) and not hasattr(mount, name)
        )
        assert not missing, (
            f"MountFS over a VirtualFS drops {missing}: those operations "
            f"shrink off a filesystem as soon as anything is mounted."
        )

    def test_does_not_claim_the_backend_key_scheme(self):
        """Key-scheme helpers describe one backend's storage, not a namespace."""
        mount = MountFS(VirtualFS({}))
        assert not [name for name in KEY_SCHEME_METHODS if hasattr(mount, name)]

    def test_resolve_path_answers_in_the_composed_namespace(self):
        base = VirtualFS({})
        base.makedirs("/work")
        mount = MountFS(base, {"/data": VirtualFS({})})
        mount.chdir("/work")
        assert mount.resolve_path("x.txt") == "/work/x.txt"
        assert mount.resolve_path("/data/y.txt") == "/data/y.txt"

    def test_utime_reaches_the_mounted_filesystem(self):
        inner = VirtualFS({})
        inner.write("note.txt", b"hi")
        mount = MountFS(VirtualFS({}), {"/data": inner})

        mount.utime("/data/note.txt", (1_577_836_800.0, 1_577_836_800.0))

        assert "2020-01-01" in inner.stat("note.txt").modified_at

    def test_get_metadata_snapshot_merges_mounts_under_their_prefix(self):
        base = VirtualFS({})
        base.write("app.py", b"x")
        inner = VirtualFS({})
        inner.write("note.txt", b"hi")
        mount = MountFS(base, {"/data": inner})

        snapshot = mount.get_metadata_snapshot()

        assert snapshot["app.py"].size == 1
        assert snapshot["data/note.txt"].size == 2

    def test_invalidate_reaches_every_backend_that_caches(self):
        state: dict[str, bytes] = {}
        base = VirtualFS(state)
        base.write("a.txt", b"one")
        mount = MountFS(base, {"/data": VirtualFS({})})

        snapshot = dict(state)
        base.write("b.txt", b"two")
        state.clear()
        state.update(snapshot)
        mount.invalidate()

        assert not mount.exists("/b.txt")

    def test_invalidate_skips_a_backend_without_caches(self, tmp_path):
        """IsolatedFS has no lazy caches and no invalidate(); that is fine."""
        mount = MountFS(IsolatedFS(root=str(tmp_path)))
        mount.invalidate()  # must not raise


class TestReadOnlyFSForwarding:
    """The wrapper's allowlist comes from the protocol, so it cannot drift."""

    @pytest.mark.parametrize(
        "name", sorted(OPTIONAL_WRITE_METHODS | DIRECT_WRITE_METHODS)
    )
    def test_mutating_optional_method_is_refused(self, name):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")
        ro = ReadOnlyFS(vfs)
        assert name in WRITE_METHODS
        with pytest.raises(PermissionError):
            getattr(ro, name)()

    @pytest.mark.parametrize(
        "name", sorted(OPTIONAL_READ_METHODS | DIRECT_READ_METHODS)
    )
    def test_reading_optional_method_is_forwarded(self, name):
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")
        ro = ReadOnlyFS(vfs)
        assert name in READ_METHODS
        forwarded = getattr(ro, name)  # PermissionError here would be the failure
        assert callable(forwarded)
        # ``access`` is mode-sensitive, so the wrapper answers it itself;
        # every other read is the backend's own bound method.
        assert name in vars(ReadOnlyFS) or forwarded == getattr(vfs, name)
