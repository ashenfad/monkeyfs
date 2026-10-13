"""``MountFS.glob`` searches every mount a pattern reaches (#36).

It used to search a mount only when the pattern started with the mount's
prefix, so ``/workspace/**/test_*.py`` with a mount at ``/workspace/docs``
returned the base's matches and nothing from the mount, while ``os.walk``,
``list(recursive=True)`` and termish's ``find`` all descend into it.

Which mounts a pattern reaches is decided by path segments, as pathlib
globs (and so ``IsolatedFS`` and the fallback for a mount without ``glob``).
The reference for most tests here is one ``IsolatedFS`` over a real tree
holding everything the composition shows: a ``MountFS`` over an
``IsolatedFS`` base must answer what that one directory answers.
"""

from __future__ import annotations

import posixpath
import sys

import pytest

from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS, VirtualFS
from monkeyfs.mount import _patterns_below

from .test_mount_optional import ListCountingFS, MinimalFS

#: What the composition shows, by the filesystem that holds it.
BASE = {
    "/workspace/test_root.py": b"",
    "/workspace/src/test_b.py": b"",
    "/workspace/src/util.py": b"",
    "/top.py": b"",
}
DOCS = {  # an IsolatedFS mounted at /workspace/docs
    "/test_top.py": b"",
    "/sub/test_m.py": b"",
    "/sub/notes.md": b"",
    "/.hidden/test_h.py": b"",
}
NESTED = {  # mounted at /workspace/docs/nested, inside the docs mount
    "/test_n.py": b"",
    "/deep/test_d.py": b"",
}
RAW = {  # an object mount at /data/sets/raw: no glob, and nothing above it
    "/test_r.py": b"",
    "/part/x.py": b"",
}
#: Held below a mount's prefix by the filesystem the mount shadows, so
#: never visible through the composition.
SHADOWED_BASE = {"/workspace/docs/ghost_test.py": b""}
SHADOWED_DOCS = {"/nested/test_shadowed.py": b""}

MOUNTS = {
    "/workspace/docs": DOCS,
    "/workspace/docs/nested": NESTED,
    "/data/sets/raw": RAW,
}


def _write_all(fs, files: dict[str, bytes]) -> None:
    for path, content in files.items():
        fs.write(path, content)


def _isolated(path, files: dict[str, bytes]) -> IsolatedFS:
    path.mkdir(parents=True, exist_ok=True)
    fs = IsolatedFS(root=str(path))
    _write_all(fs, files)
    return fs


@pytest.fixture
def composed(tmp_path) -> MountFS:
    base = _isolated(tmp_path / "base", {**BASE, **SHADOWED_BASE})
    docs = _isolated(tmp_path / "docs", {**DOCS, **SHADOWED_DOCS})
    nested = _isolated(tmp_path / "nested", NESTED)
    return MountFS(
        base,
        {
            "/workspace/docs": docs,
            "/workspace/docs/nested": ReadOnlyFS(nested),
            "/data/sets/raw": MinimalFS(RAW),
        },
    )


@pytest.fixture
def reference(tmp_path) -> IsolatedFS:
    """One real directory holding exactly what the composition shows."""
    fs = _isolated(tmp_path / "reference", BASE)
    for prefix, files in MOUNTS.items():
        _write_all(fs, {prefix + path: content for path, content in files.items()})
    return fs


#: Patterns whose answer pathlib gives the same way on every supported
#: Python (a trailing ``**`` matched directories only before 3.13).
PATTERNS = [
    # From above a mount
    "/workspace/**/test_*.py",
    "/workspace/*/test_*.py",
    "/**/test_*.py",
    "/workspace/test_*.py",
    "/*/*/test_*.py",
    "/workspace/d?cs/*.py",
    "/workspace/[cd]ocs/test_*.py",
    "/**/sub/*.py",
    "/**/docs/**/test_*.py",
    # ** crossing more than one prefix, into a nested mount
    "/workspace/**/nested/**/test_*.py",
    "/**/deep/*.py",
    "/workspace/*/nested/*.py",
    "/workspace/**/test_[nd].py",
    # At a mount's own root, and inside one
    "/workspace/docs/**/test_*.py",
    "/workspace/docs/*.py",
    "/workspace/docs/*",
    "/workspace/docs/nested/*",
    # Directories on the way to a mount, which no filesystem holds
    "/*",
    "/*/*",
    "/data/*",
    "/data/*/raw",
    "/data/*/raw/*.py",
    "/data/**/test_*.py",
    "/d*/s*/r*/p*/*.py",
    # No wildcard at all
    "/workspace/docs",
    "/workspace/docs/nested",
    "/data",
    "/data/sets/raw/test_r.py",
    # Reaching no mount, or nothing at all
    "/workspace/src/*.py",
    "/workspace/src/**/test_*.py",
    "/nope/**/x.py",
    "/data/*/nope/*",
]


class TestAgainstOneRealTree:
    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_absolute(self, composed, reference, pattern):
        assert composed.glob(pattern) == reference.glob(pattern)

    @pytest.mark.skipif(
        sys.version_info < (3, 13),
        reason="pathlib's trailing ** matched directories only before 3.13",
    )
    @pytest.mark.parametrize(
        "pattern", ["/workspace/**", "/data/**", "/workspace/docs/**", "/**"]
    )
    def test_a_trailing_double_star(self, composed, reference, pattern):
        assert composed.glob(pattern) == reference.glob(pattern)

    @pytest.mark.parametrize(
        "cwd, pattern",
        [
            ("/workspace", "**/test_*.py"),
            ("/workspace", "test_*.py"),
            ("/workspace", "*/test_*.py"),
            ("/workspace", "docs/*.py"),
            ("/workspace", "*"),
            ("/workspace/docs", "*.py"),
            ("/workspace/docs", "**/test_*.py"),
            ("/workspace/docs", "nested/*.py"),
            ("/workspace/docs/sub", "../nested/**/*.py"),
            ("/data", "*/raw/*.py"),
            ("/data", "**/*.py"),
        ],
    )
    def test_relative_to_a_working_directory(self, composed, reference, cwd, pattern):
        """``MountFS`` resolves a relative pattern against its working
        directory and answers in absolute paths, as it always has. The
        reference is asked the same pattern made absolute."""
        composed.chdir(cwd)
        expected = reference.glob(posixpath.normpath(f"{cwd}/{pattern}"))
        assert composed.glob(pattern) == expected

    def test_the_shadowed_content_stays_hidden(self, composed):
        found = composed.glob("/**/*.py")
        assert "/workspace/docs/ghost_test.py" not in found
        assert "/workspace/docs/nested/test_shadowed.py" not in found
        assert "/workspace/docs/nested/test_n.py" in found


class TestTheIssueRepro:
    """The table in #36: a VirtualFS base, an IsolatedFS mount."""

    @pytest.fixture
    def fs(self, tmp_path):
        mounted = _isolated(
            tmp_path / "docs", {"/sub/test_m.py": b"x", "/test_top.py": b"x"}
        )
        base = VirtualFS({})
        base.makedirs("workspace/src")
        base.write("workspace/src/test_b.py", b"x")
        return MountFS(base, {"/workspace/docs": mounted})

    @pytest.mark.parametrize(
        "pattern, expected",
        [
            (
                "/workspace/**/test_*.py",
                [
                    "/workspace/docs/sub/test_m.py",
                    "/workspace/docs/test_top.py",
                    "/workspace/src/test_b.py",
                ],
            ),
            (
                "/workspace/*/test_*.py",
                ["/workspace/docs/test_top.py", "/workspace/src/test_b.py"],
            ),
            (
                "/**/test_*.py",
                [
                    "/workspace/docs/sub/test_m.py",
                    "/workspace/docs/test_top.py",
                    "/workspace/src/test_b.py",
                ],
            ),
            (
                "/workspace/docs/**/test_*.py",
                ["/workspace/docs/sub/test_m.py", "/workspace/docs/test_top.py"],
            ),
        ],
    )
    def test_the_table(self, fs, pattern, expected):
        assert fs.glob(pattern) == expected


class TestNontainerTestDiscovery:
    """``ws-pytest`` and ``ws-vitest`` find test files with this pair, from
    whatever directory they are run in. Run from above a mount, the pair
    must find the tests inside it, whatever backs the mount."""

    TESTS = {"/test_top.py": b"", "/pkg/test_deep.py": b"", "/pkg/util.py": b""}

    @staticmethod
    def _discover(fs, base: str) -> set[str]:
        return set(fs.glob(f"{base}/test_*.py")) | set(fs.glob(f"{base}/**/test_*.py"))

    @pytest.mark.parametrize("backing", ["isolated", "object", "virtual"])
    @pytest.mark.parametrize("base", ["/workspace", "/workspace/docs", ""])
    def test_from_above_and_at_the_mount(self, tmp_path, backing, base):
        if backing == "isolated":
            mounted = _isolated(tmp_path / "docs", self.TESTS)
        elif backing == "object":
            mounted = ReadOnlyFS(MinimalFS(self.TESTS))
        else:
            mounted = VirtualFS({})
            _write_all(mounted, self.TESTS)
        root = VirtualFS({})
        root.write("workspace/test_mine.py", b"")
        fs = MountFS(root, {"/workspace/docs": mounted})

        found = self._discover(fs, base)

        assert {
            "/workspace/docs/test_top.py",
            "/workspace/docs/pkg/test_deep.py",
        } <= found
        assert "/workspace/docs/pkg/util.py" not in found
        if base != "/workspace/docs":
            assert "/workspace/test_mine.py" in found


class TestWhichMountsAreAsked:
    def test_a_pattern_that_cannot_reach_a_mount_never_asks_it(self):
        inner = ListCountingFS({"/test_x.py": b""})
        fs = MountFS(VirtualFS({}), {"/workspace/docs": inner})
        for pattern in (
            "/workspace/src/**/test_*.py",
            "/workspace/test_*.py",
            "/other/**",
            "/workspace/d",
            "/*/x",
        ):
            fs.glob(pattern)
        assert inner.listed == []

    def test_a_pattern_that_reaches_a_mount_asks_it_for_the_rest(self):
        inner = ListCountingFS({"/sub/test_x.py": b""})
        fs = MountFS(VirtualFS({}), {"/workspace/docs": inner})
        assert fs.glob("/workspace/*/sub/*.py") == ["/workspace/docs/sub/test_x.py"]
        assert inner.listed == [("/sub", True)]

    @pytest.mark.parametrize(
        "pattern, prefix, expected",
        [
            ("/workspace/*/test_*.py", "/workspace/docs", {("test_*.py",)}),
            (
                "/workspace/**/test_*.py",
                "/workspace/docs",
                {("**", "test_*.py"), ("test_*.py",)},
            ),
            ("/workspace/docs/*.py", "/workspace/docs", {("*.py",)}),
            ("/workspace/docs", "/workspace/docs", {()}),
            ("/workspace/*", "/workspace/docs", {()}),
            ("/workspace/test_*.py", "/workspace/docs", set()),
            ("/workspace", "/workspace/docs", set()),
            ("/*/*/x", "/a/b/c", set()),
            (
                "/**/b/**/x",
                "/a/b",
                {("**", "b", "**", "x"), ("b", "**", "x"), ("**", "x"), ("x",)},
            ),
            ("/a/**", "/a/b/c", {("**",), ()}),
        ],
    )
    def test_the_share_each_mount_is_handed(self, pattern, prefix, expected):
        parts = [part for part in pattern.split("/") if part]
        assert _patterns_below(parts, prefix.strip("/").split("/")) == expected
