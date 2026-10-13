"""``VirtualFS.glob`` matches by path segment, as ``IsolatedFS`` does (#38).

It ran ``fnmatch`` over each file's whole path, so ``*`` crossed ``/``, a
``**`` needed at least one directory, and no directory ever matched. Now
it answers what pathlib answers on Python 3.13 and later: ``*``, ``?``
and ``[...]`` within one segment, ``**`` for zero or more directories (a
trailing one includes the directory itself), hidden names matched, and
directories returned as well as files.
"""

from __future__ import annotations

import fnmatch
import posixpath
import sys

import pytest

from monkeyfs import IsolatedFS, MountFS, VirtualFS

TREE = {
    "/test_top.py": b"",
    "/x.py": b"",
    "/src/test_a.py": b"",
    "/src/x.py": b"",
    "/src/.env": b"",
    "/src/pkg/test_b.py": b"",
    "/src/pkg/x.py": b"",
    "/.hidden/x.py": b"",
    "/a/b": b"",
}
EMPTY_DIRS = ["/empty"]

_TRAILING = pytest.mark.skipif(
    sys.version_info < (3, 13),
    reason="pathlib's trailing ** matched directories only before 3.13",
)

#: (pattern, what VirtualFS answers on every Python). Patterns marked
#: trailing are compared with IsolatedFS on 3.13+ only.
ABSOLUTE = [
    ("/*", ["/.hidden", "/a", "/empty", "/src", "/test_top.py", "/x.py"]),
    ("/*/x.py", ["/.hidden/x.py", "/src/x.py"]),
    ("/**/x.py", ["/.hidden/x.py", "/src/pkg/x.py", "/src/x.py", "/x.py"]),
    ("/**/*/x.py", ["/.hidden/x.py", "/src/pkg/x.py", "/src/x.py"]),
    ("/*/*/test_*.py", ["/src/pkg/test_b.py"]),
    ("/**/test_*.py", ["/src/pkg/test_b.py", "/src/test_a.py", "/test_top.py"]),
    ("/**/pkg", ["/src/pkg"]),
    (
        "/src/**",
        [
            "/src",
            "/src/.env",
            "/src/pkg",
            "/src/pkg/test_b.py",
            "/src/pkg/x.py",
            "/src/test_a.py",
            "/src/x.py",
        ],
    ),
    ("/src/pkg/**", ["/src/pkg", "/src/pkg/test_b.py", "/src/pkg/x.py"]),
    ("/?", ["/a"]),
    ("/a/?", ["/a/b"]),
    ("/src/?.py", ["/src/x.py"]),
    ("/src/[pt]*", ["/src/pkg", "/src/test_a.py"]),
    ("/src/[!p]*.py", ["/src/test_a.py", "/src/x.py"]),
    ("/.hidden/*", ["/.hidden/x.py"]),
    ("/src/.*", ["/src/.env"]),
    ("/src/*", ["/src/.env", "/src/pkg", "/src/test_a.py", "/src/x.py"]),
    ("/src/pkg/x.py", ["/src/pkg/x.py"]),
    ("/src/pkg", ["/src/pkg"]),
    ("/empty", ["/empty"]),
    ("/empty/*", []),
    ("/src/*.rs", []),
    ("/nope/*.py", []),
    ("/nope", []),
    ("/x.py/*", []),
]
TRAILING = {"/src/**", "/src/pkg/**"}

#: (cwd, relative pattern, answer)
RELATIVE = [
    ("/src", "*.py", ["test_a.py", "x.py"]),
    ("/src", "*", [".env", "pkg", "test_a.py", "x.py"]),
    ("/src", "**/x.py", ["pkg/x.py", "x.py"]),
    ("/src", "*/x.py", ["pkg/x.py"]),
    ("/src", "pkg/*", ["pkg/test_b.py", "pkg/x.py"]),
    ("/src", "../*.py", ["../test_top.py", "../x.py"]),
    ("/src/pkg", "*.py", ["test_b.py", "x.py"]),
    ("/src/pkg", "*.rs", []),
    ("/", "*/x.py", [".hidden/x.py", "src/x.py"]),
    ("/", "**/test_*.py", ["src/pkg/test_b.py", "src/test_a.py", "test_top.py"]),
    (
        "/src",
        "**",
        [".", ".env", "pkg", "pkg/test_b.py", "pkg/x.py", "test_a.py", "x.py"],
    ),
    ("/src/pkg", "**", [".", "test_b.py", "x.py"]),
    ("/", "a/**", ["a", "a/b"]),
]


def _fill(fs):
    for path, content in TREE.items():
        fs.write(path, content)
    for path in EMPTY_DIRS:
        fs.mkdir(path)
    return fs


@pytest.fixture
def virtual() -> VirtualFS:
    return _fill(VirtualFS({}))


@pytest.fixture
def isolated(tmp_path) -> IsolatedFS:
    return _fill(IsolatedFS(str(tmp_path)))


def _params(rows, trailing_at):
    return [
        pytest.param(*row, marks=[_TRAILING] if trailing_at(row) else [])
        for row in rows
    ]


class TestTheAnswers:
    """What ``VirtualFS`` answers, on every Python."""

    @pytest.mark.parametrize("pattern, expected", ABSOLUTE)
    def test_absolute(self, virtual, pattern, expected):
        assert virtual.glob(pattern) == expected

    @pytest.mark.parametrize("cwd, pattern, expected", RELATIVE)
    def test_relative(self, virtual, cwd, pattern, expected):
        virtual.chdir(cwd)
        assert virtual.glob(pattern) == expected

    def test_the_issue_table(self):
        tree = {"/src/test_a.py": b"", "/src/pkg/test_b.py": b"", "/test_top.py": b""}
        v = VirtualFS({})
        for path, content in tree.items():
            v.write(path, content)
        assert v.glob("/*/test_*.py") == ["/src/test_a.py"]
        assert v.glob("/*") == ["/src", "/test_top.py"]
        assert v.glob("/**/test_*.py") == [
            "/src/pkg/test_b.py",
            "/src/test_a.py",
            "/test_top.py",
        ]
        assert v.glob("/src/**") == [
            "/src",
            "/src/pkg",
            "/src/pkg/test_b.py",
            "/src/test_a.py",
        ]

    def test_only_the_fixed_leading_directory_is_listed(self):
        class Counting(VirtualFS):
            def __init__(self):
                super().__init__({})
                self.listed: list[tuple[str, bool]] = []

            def list(self, path=".", recursive=False):
                self.listed.append((path, recursive))
                return super().list(path, recursive)

        fs = _fill(Counting())
        fs.listed.clear()
        assert fs.glob("/src/pkg/*.py") == ["/src/pkg/test_b.py", "/src/pkg/x.py"]
        assert fs.listed == [("/src/pkg", True)]
        fs.listed.clear()
        assert fs.glob("/src/pkg/x.py") == ["/src/pkg/x.py"]
        assert fs.listed == []


class TestAgainstIsolatedFS:
    """The same tree in a ``VirtualFS`` and an ``IsolatedFS`` gives the same
    answer. A trailing ``**`` is compared on 3.13+, where pathlib has the
    semantics both match."""

    @pytest.mark.parametrize(
        "pattern, expected", _params(ABSOLUTE, lambda row: row[0] in TRAILING)
    )
    def test_absolute(self, virtual, isolated, pattern, expected):
        assert virtual.glob(pattern) == isolated.glob(pattern) == expected

    @pytest.mark.parametrize(
        "cwd, pattern, expected", _params(RELATIVE, lambda row: "**" in row[1])
    )
    def test_relative(self, virtual, isolated, cwd, pattern, expected):
        """Compared with ``IsolatedFS``'s answer to the same pattern made
        absolute, rather than to the relative one, which finds nothing
        from a cwd other than ``/`` until #39 is fixed."""
        virtual.chdir(cwd)
        absolute = isolated.glob(posixpath.join(cwd, pattern))
        assert virtual.glob(pattern) == sorted(
            posixpath.relpath(path, cwd) for path in absolute
        )


class TestUnderMountFS:
    """The cases from #38: a ``VirtualFS`` base, which is how nontainer
    composes a workspace, with an ``IsolatedFS`` mounted in it. One answer
    no longer mixes two dialects."""

    @pytest.fixture
    def fs(self, tmp_path) -> MountFS:
        base = VirtualFS({})
        base.write("/workspace/src/pkg/test_b.py", b"")
        base.write("/workspace/test_w.py", b"")
        docs = IsolatedFS(str(tmp_path / "docs"))
        docs.write("/pkg/test_m.py", b"")
        docs.write("/test_d.py", b"")
        return MountFS(base, {"/workspace/docs": docs})

    @pytest.fixture
    def reference(self, tmp_path) -> IsolatedFS:
        fs = IsolatedFS(str(tmp_path / "reference"))
        for path in (
            "/workspace/src/pkg/test_b.py",
            "/workspace/test_w.py",
            "/workspace/docs/pkg/test_m.py",
            "/workspace/docs/test_d.py",
        ):
            fs.write(path, b"")
        return fs

    @pytest.mark.parametrize(
        "pattern, expected",
        [
            # Two levels deep in the base used to match; neither does.
            ("/workspace/*/test_*.py", ["/workspace/docs/test_d.py"]),
            # The base's /workspace/src now matches, as the mount's root does,
            # and the file two levels down no longer does.
            (
                "/workspace/*",
                ["/workspace/docs", "/workspace/src", "/workspace/test_w.py"],
            ),
            (
                "/workspace/*/pkg/test_*.py",
                ["/workspace/docs/pkg/test_m.py", "/workspace/src/pkg/test_b.py"],
            ),
            (
                "/workspace/**/test_*.py",
                [
                    "/workspace/docs/pkg/test_m.py",
                    "/workspace/docs/test_d.py",
                    "/workspace/src/pkg/test_b.py",
                    "/workspace/test_w.py",
                ],
            ),
            ("/**/pkg", ["/workspace/docs/pkg", "/workspace/src/pkg"]),
            ("/workspace/test_*.py", ["/workspace/test_w.py"]),
        ],
    )
    def test_the_composition(self, fs, reference, pattern, expected):
        assert fs.glob(pattern) == reference.glob(pattern) == expected


def fnmatch_glob(fs, pattern: str) -> list[str]:
    """``fnmatch`` over every file's whole path: how ``VirtualFS.glob``
    matched before #38, and how a backend can still match (nontainer's
    AgentFS filesystem does). ``*`` crosses ``/`` and a ``**`` needs a
    directory."""
    cwd = fs.getcwd()
    absolute = pattern.startswith("/")
    full = pattern if absolute else posixpath.join(cwd, pattern)
    found = []
    for rel in fs.list("/", recursive=True):
        path = "/" + rel
        if fs.isfile(path) and fnmatch.fnmatch(path, full):
            found.append(path if absolute else posixpath.relpath(path, cwd))
    return sorted(found)


class FnmatchGlobFS(VirtualFS):
    """A ``VirtualFS`` whose ``glob`` is the whole-path ``fnmatch`` one."""

    def glob(self, pattern: str) -> list[str]:
        return fnmatch_glob(self, pattern)


class TestAMountWhoseGlobNeedsADirectoryForDoubleStar:
    """``MountFS`` hands a mount the zero-length match of a ``**`` that
    carries into it as a pattern of its own. Every backend in monkeyfs
    matches ``**`` against zero directories by itself now, but a backend
    that globs with ``fnmatch`` over the whole path doesn't, and without
    that hand-over it would miss the files at its root."""

    def test_the_double_is_that_dialect(self):
        fs = FnmatchGlobFS({})
        fs.write("/x.py", b"")
        fs.write("/d/x.py", b"")
        assert fs.glob("/**/x.py") == ["/d/x.py"]

    @pytest.mark.parametrize(
        "pattern", ["/a/**/x", "/**/x", "/a/**/**/x", "/**/a/**/**/x"]
    )
    def test_files_at_the_mount_root_are_found(self, pattern):
        mounted = FnmatchGlobFS({})
        for path in ("/x", "/d/x", "/d/e/x"):
            mounted.write(path, b"")
        fs = MountFS(VirtualFS({}), {"/a": mounted})
        assert fs.glob(pattern) == ["/a/d/e/x", "/a/d/x", "/a/x"]
