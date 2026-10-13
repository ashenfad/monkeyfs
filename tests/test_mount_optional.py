"""A mount with only the required reads still works behind ``MountFS``.

``MountFS`` defines every optional read, so the patch layer always finds one
there. It used to pass each call on to the owning filesystem regardless, and
a backend without the method failed with ``AttributeError`` as soon as stdlib
code reached it -- ``os.walk``, a recursive ``glob`` and ``Path.rglob`` all
ask ``islink``. The answers are now derived from the required reads where
they can be, and ``NotImplementedError`` (the protocol's contract for a
missing optional method) is raised where they cannot.
"""

from __future__ import annotations

import errno
import glob
import os
import posixpath
import sys
from pathlib import Path

import pytest

from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS, VirtualFS, patch
from monkeyfs.base import FileMetadata

STAMP = "2026-01-01T00:00:00+00:00"
MOUNT = "/workspace/docs"

FILES = {
    "/a.md": b"# a",
    "/notes/b.md": b"# bee",
    "/notes/deep/c.md": b"# sea, deeper",
    "/notes/data.csv": b"x,y\n1,2\n",
}


class MinimalFS:
    """The required reads and ``read``, and nothing else.

    No ``islink``, ``getsize``, ``samefile``, ``readlink``, ``access``,
    ``open``, ``glob`` or anything to write with: the shape of an arbitrary
    object an embedder mounts read-only.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self._files = dict(FILES if files is None else files)
        self._dirs = {"/"}
        for path in self._files:
            parent = posixpath.dirname(path)
            while parent not in self._dirs:
                self._dirs.add(parent)
                parent = posixpath.dirname(parent)
        self._cwd = "/"

    def _abs(self, path: str) -> str:
        return posixpath.normpath(posixpath.join(self._cwd, path))

    def getcwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        if not self.isdir(path):
            raise FileNotFoundError(path)
        self._cwd = self._abs(path)

    def exists(self, path: str) -> bool:
        return self.isfile(path) or self.isdir(path)

    def isfile(self, path: str) -> bool:
        return self._abs(path) in self._files

    def isdir(self, path: str) -> bool:
        return self._abs(path) in self._dirs

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        root = self._abs(path)
        if root not in self._dirs:
            raise FileNotFoundError(path)
        prefix = root.rstrip("/") + "/"
        names = set()
        for entry in self._files.keys() | self._dirs:
            if entry == root or not entry.startswith(prefix):
                continue
            rel = entry[len(prefix) :]
            if recursive or "/" not in rel:
                names.add(rel)
        return sorted(names)

    def stat(self, path: str) -> FileMetadata:
        target = self._abs(path)
        if target in self._dirs:
            return FileMetadata(0, STAMP, STAMP, is_dir=True)
        if target in self._files:
            return FileMetadata(len(self._files[target]), STAMP, STAMP)
        raise FileNotFoundError(path)

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        target = self._abs(path)
        if target not in self._files:
            raise FileNotFoundError(path)
        data = self._files[target][offset:]
        return data if size < 0 else data[:size]


class LinkingFS(MinimalFS):
    """Has symlinks (``islink`` answers for them) but no ``samefile`` or
    ``readlink``: path identity no longer settles file identity here."""

    def __init__(self) -> None:
        super().__init__()
        self._files["/link.md"] = self._files["/a.md"]

    def islink(self, path: str) -> bool:
        return self._abs(path) == "/link.md"


class StubbedFS(MinimalFS):
    """Declares the optional reads but raises ``NotImplementedError`` from
    them, the protocol's way of saying it does not implement them."""

    def getsize(self, path: str) -> int:
        raise NotImplementedError("getsize")

    def islink(self, path: str) -> bool:
        raise NotImplementedError("islink")


@pytest.fixture(params=["bare", "read-only"])
def mounted(request):
    """A ``MountFS`` over a VirtualFS base, with a ``MinimalFS`` mounted at
    ``/workspace/docs`` -- as it is, and wrapped in ``ReadOnlyFS`` the way
    an embedder lends one out."""
    inner = MinimalFS()
    mount = inner if request.param == "bare" else ReadOnlyFS(inner)
    base = VirtualFS({})
    base.write("/workspace/main.py", b"print('hi')")
    return MountFS(base, {MOUNT: mount})


def _under(path: str) -> str:
    return MOUNT + path


class TestReadOnlyFSSeesThrough:
    """``ReadOnlyFS`` must not invent an optional method its backend lacks,
    or ``MountFS`` would call it instead of falling back."""

    @pytest.mark.parametrize("name", ["getsize", "islink", "samefile", "readlink"])
    def test_a_missing_optional_read_is_missing_through_the_wrapper(self, name):
        assert getattr(ReadOnlyFS(MinimalFS()), name, None) is None

    def test_access_says_not_implemented_rather_than_attribute_error(self):
        with pytest.raises(NotImplementedError, match="access"):
            ReadOnlyFS(MinimalFS()).access("/a.md", os.R_OK)

    def test_access_still_refuses_writes_without_asking_the_backend(self):
        assert ReadOnlyFS(MinimalFS()).access("/a.md", os.W_OK) is False

    def test_under_patch_access_is_not_implemented(self):
        with patch(ReadOnlyFS(MinimalFS())):
            with pytest.raises(NotImplementedError):
                os.access("/a.md", os.R_OK)


class TestStdlibOverAMinimalMount:
    """The calls from ashenfad/monkeyfs#33, under ``patch()``."""

    def test_os_walk(self, mounted):
        with patch(mounted):
            walked = {
                root: (sorted(dirs), sorted(files))
                for root, dirs, files in os.walk(MOUNT)
            }
        assert walked == {
            MOUNT: (["notes"], ["a.md"]),
            _under("/notes"): (["deep"], ["b.md", "data.csv"]),
            _under("/notes/deep"): ([], ["c.md"]),
        }

    def test_os_walk_from_above_the_mount(self, mounted):
        with patch(mounted):
            files = {
                os.path.join(root, name)
                for root, _, names in os.walk("/workspace")
                for name in names
            }
        assert files == {"/workspace/main.py"} | {_under(p) for p in FILES}

    def test_recursive_glob(self, mounted):
        with patch(mounted):
            found = glob.glob(f"{MOUNT}/**/*.md", recursive=True)
        assert sorted(found) == [
            _under("/a.md"),
            _under("/notes/b.md"),
            _under("/notes/deep/c.md"),
        ]

    def test_path_rglob(self, mounted):
        with patch(mounted):
            found = sorted(str(p) for p in Path(MOUNT).rglob("*.md"))
        assert found == [
            _under("/a.md"),
            _under("/notes/b.md"),
            _under("/notes/deep/c.md"),
        ]

    def test_os_path_islink(self, mounted):
        with patch(mounted):
            assert os.path.islink(_under("/a.md")) is False
            assert os.path.islink(_under("/notes")) is False
            assert os.path.islink(_under("/missing.md")) is False

    def test_os_path_getsize(self, mounted):
        with patch(mounted):
            assert os.path.getsize(_under("/notes/deep/c.md")) == len(b"# sea, deeper")
            with pytest.raises(FileNotFoundError):
                os.path.getsize(_under("/missing.md"))

    def test_open_and_read(self, mounted):
        with patch(mounted):
            with open(_under("/notes/data.csv")) as handle:
                assert handle.read() == "x,y\n1,2\n"


class TestDerivedAnswers:
    """What ``MountFS`` answers for a mount without the method."""

    def test_readlink_is_einval_on_an_existing_path(self, mounted):
        with pytest.raises(OSError) as caught:
            mounted.readlink(_under("/a.md"))
        assert caught.value.errno == errno.EINVAL

    def test_readlink_on_a_missing_path_is_file_not_found(self, mounted):
        with pytest.raises(FileNotFoundError):
            mounted.readlink(_under("/missing.md"))

    def test_samefile_is_path_identity(self, mounted):
        assert mounted.samefile(_under("/a.md"), _under("/notes/../a.md"))
        assert not mounted.samefile(_under("/a.md"), _under("/notes/b.md"))

    def test_samefile_on_a_missing_path_is_false(self, mounted):
        assert not mounted.samefile(_under("/missing.md"), _under("/missing.md"))

    def test_access_answers_existence_and_reading(self, mounted):
        assert mounted.access(_under("/a.md"), os.F_OK)
        assert mounted.access(_under("/notes"), os.R_OK)
        assert not mounted.access(_under("/missing.md"), os.F_OK)
        assert not mounted.access(_under("/missing.md"), os.R_OK)

    def test_access_cannot_answer_execute(self, mounted):
        with pytest.raises(NotImplementedError, match="access"):
            mounted.access(_under("/a.md"), os.X_OK)

    def test_write_access_on_a_bare_mount_is_not_implemented(self):
        fs = MountFS(VirtualFS({}), {MOUNT: MinimalFS()})
        with pytest.raises(NotImplementedError, match="access"):
            fs.access(_under("/a.md"), os.W_OK)

    def test_write_access_on_a_read_only_mount_is_refused(self):
        fs = MountFS(VirtualFS({}), {MOUNT: ReadOnlyFS(MinimalFS())})
        assert fs.access(_under("/a.md"), os.W_OK) is False
        with patch(fs):
            assert os.access(_under("/a.md"), os.W_OK) is False
            assert os.access(_under("/a.md"), os.R_OK) is True


class TestMountsWithSomeOfTheMethods:
    def test_a_mount_with_links_but_no_samefile_cannot_compare_paths(self):
        fs = MountFS(VirtualFS({}), {MOUNT: LinkingFS()})
        assert fs.samefile(_under("/a.md"), _under("/a.md"))
        with pytest.raises(NotImplementedError, match="samefile"):
            fs.samefile(_under("/link.md"), _under("/a.md"))

    def test_a_mount_with_links_but_no_readlink_cannot_read_one(self):
        fs = MountFS(VirtualFS({}), {MOUNT: LinkingFS()})
        with pytest.raises(NotImplementedError, match="readlink"):
            fs.readlink(_under("/link.md"))
        with pytest.raises(OSError) as caught:
            fs.readlink(_under("/a.md"))
        assert caught.value.errno == errno.EINVAL

    def test_a_mount_that_raises_not_implemented_gets_the_fallback(self):
        fs = MountFS(VirtualFS({}), {MOUNT: StubbedFS()})
        assert fs.getsize(_under("/a.md")) == len(b"# a")
        assert fs.islink(_under("/a.md")) is False
        with patch(fs):
            assert len(list(os.walk(MOUNT))) == 3

    def test_an_islink_stub_does_not_make_a_mount_have_links(self):
        """A stub that raises NotImplementedError is a missing ``islink``,
        here as everywhere: two different paths are two different files."""
        fs = MountFS(VirtualFS({}), {MOUNT: StubbedFS()})
        assert not fs.samefile(_under("/a.md"), _under("/notes/b.md"))
        with pytest.raises(OSError) as caught:
            fs.readlink(_under("/a.md"))
        assert caught.value.errno == errno.EINVAL

    def test_an_islink_stub_on_a_patched_backend_reads_as_no_link(self):
        """Not behind a MountFS: the patch layer's lenient islink probe,
        which os.scandir uses, treats the stub as no links too."""
        with patch(StubbedFS()):
            with os.scandir("/") as entries:
                links = {entry.name: entry.is_symlink() for entry in entries}
        assert links == {"a.md": False, "notes": False}

    def test_a_mount_with_the_method_is_still_asked(self):
        class Sized(MinimalFS):
            def getsize(self, path: str) -> int:
                return 42

        fs = MountFS(VirtualFS({}), {MOUNT: Sized()})
        assert fs.getsize(_under("/a.md")) == 42


GLOB_TREE = {
    "/x.py": b"top",
    "/a/x.py": b"a",
    "/a/n.md": b"note",
    "/a/.h.md": b"hidden",
    "/a/b/x.py": b"deep",
    "/.hid/x.py": b"in a hidden directory",
    "/c/d/e.txt": b"far down",
}

#: Patterns whose answer pathlib -- and so ``IsolatedFS`` -- gives the same
#: way on every supported Python.
STABLE_PATTERNS = [
    "/**/x.py",
    "/*/x.py",
    "/a/*",
    "/a/*.md",
    "/a/**/*.md",
    "/**/*.md",
    "/?/x.py",
    "/a/[xn]*",
    "/a/x.py",
    "/a",
    "/a/nope.py",
    "/nope/*",
    "/nope/**/x.py",
]


class ListCountingFS(MinimalFS):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__(files)
        self.listed: list[tuple[str, bool]] = []

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        self.listed.append((path, recursive))
        return super().list(path, recursive)


def _isolated_tree(root: Path) -> IsolatedFS:
    fs = IsolatedFS(root=str(root))
    for path, content in GLOB_TREE.items():
        fs.write(path, content)
    return fs


def _wrap(fs, how: str):
    return fs if how == "bare" else ReadOnlyFS(fs)


class TestGlobOverAMinimalMount:
    """``MountFS.glob`` on a mount with no ``glob`` answers from ``list()``,
    the way a mount whose backend has ``glob`` answers."""

    @pytest.mark.parametrize("how", ["bare", "read-only"])
    @pytest.mark.parametrize("pattern", STABLE_PATTERNS)
    def test_matches_a_mount_that_has_glob(self, tmp_path, how, pattern):
        minimal = MountFS(VirtualFS({}), {MOUNT: _wrap(MinimalFS(GLOB_TREE), how)})
        globbing = MountFS(VirtualFS({}), {MOUNT: _wrap(_isolated_tree(tmp_path), how)})
        assert minimal.glob(_under(pattern)) == globbing.glob(_under(pattern))

    @pytest.mark.parametrize("how", ["bare", "read-only"])
    def test_the_answers_themselves(self, how):
        fs = MountFS(VirtualFS({}), {MOUNT: _wrap(MinimalFS(GLOB_TREE), how)})
        expect = {
            "/**/x.py": ["/.hid/x.py", "/a/b/x.py", "/a/x.py", "/x.py"],
            "/*/x.py": ["/.hid/x.py", "/a/x.py"],
            "/a/*.md": ["/a/.h.md", "/a/n.md"],
            "/a/**": ["/a", "/a/.h.md", "/a/b", "/a/b/x.py", "/a/n.md", "/a/x.py"],
            "/a/x.py": ["/a/x.py"],
            "/a/nope.py": [],
            "/nope/*": [],
        }
        for pattern, matches in expect.items():
            assert fs.glob(_under(pattern)) == [_under(m) for m in matches], pattern

    @pytest.mark.skipif(
        sys.version_info < (3, 13),
        reason="pathlib's trailing ** matched directories only before 3.13",
    )
    def test_a_trailing_double_star_matches_a_mount_that_has_glob(self, tmp_path):
        minimal = MountFS(VirtualFS({}), {MOUNT: MinimalFS(GLOB_TREE)})
        globbing = MountFS(VirtualFS({}), {MOUNT: _isolated_tree(tmp_path)})
        assert minimal.glob(_under("/a/**")) == globbing.glob(_under("/a/**"))

    def test_walks_only_from_the_fixed_leading_directory(self):
        inner = ListCountingFS(GLOB_TREE)
        fs = MountFS(VirtualFS({}), {MOUNT: inner})
        fs.glob(_under("/a/b/*.py"))
        assert inner.listed == [("/a/b", True)]

    def test_a_pattern_with_no_wildcard_lists_nothing(self):
        inner = ListCountingFS(GLOB_TREE)
        fs = MountFS(VirtualFS({}), {MOUNT: inner})
        assert fs.glob(_under("/a/n.md")) == [_under("/a/n.md")]
        assert inner.listed == []

    def test_the_test_file_search_from_nontainer(self):
        """``ws-pytest`` looks for test files this way, under an object mount."""
        tree = {"/test_top.py": b"", "/pkg/test_deep.py": b"", "/pkg/util.py": b""}
        fs = MountFS(VirtualFS({}), {MOUNT: ReadOnlyFS(MinimalFS(tree))})
        found = set(fs.glob(f"{MOUNT}/test_*.py")) | set(
            fs.glob(f"{MOUNT}/**/test_*.py")
        )
        assert found == {_under("/test_top.py"), _under("/pkg/test_deep.py")}
