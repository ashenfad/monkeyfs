"""Tests for IsolatedFS direct API."""

import os

import pytest

from monkeyfs import IsolatedFS
from monkeyfs.base import FileInfo, FileMetadata

# ---------------------------------------------------------------------------
# Core read/write
# ---------------------------------------------------------------------------


class TestIsolatedCoreReadWrite:
    """Test basic read/write operations on IsolatedFS."""

    def test_write_and_read(self, tmp_path):
        """Test writing bytes and reading them back."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("hello.txt", b"hello world")
        assert fs.read("hello.txt") == b"hello world"

    def test_write_creates_parents(self, tmp_path):
        """Test that write auto-creates parent directories."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("a/b/c.txt", b"deep")
        assert fs.read("a/b/c.txt") == b"deep"
        assert fs.isdir("a") is True
        assert fs.isdir("a/b") is True

    def test_write_append(self, tmp_path):
        """Test that write with mode='a' appends content."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("log.txt", b"line1\n")
        fs.write("log.txt", b"line2\n", mode="a")
        assert fs.read("log.txt") == b"line1\nline2\n"

    def test_read_nonexistent_raises(self, tmp_path):
        """Test that reading a missing file raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.read("missing.txt")

    def test_write_many(self, tmp_path):
        """Test writing multiple files at once."""
        fs = IsolatedFS(str(tmp_path))
        fs.write_many(
            {
                "a.txt": b"alpha",
                "b.txt": b"bravo",
                "c.txt": b"charlie",
            }
        )
        assert fs.read("a.txt") == b"alpha"
        assert fs.read("b.txt") == b"bravo"
        assert fs.read("c.txt") == b"charlie"

    def test_remove(self, tmp_path):
        """Test removing a file."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("temp.txt", b"gone soon")
        assert fs.exists("temp.txt") is True
        fs.remove("temp.txt")
        assert fs.exists("temp.txt") is False

    def test_remove_nonexistent_raises(self, tmp_path):
        """Test that removing a missing file raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.remove("nope.txt")

    def test_remove_directory_raises(self, tmp_path):
        """Test that removing a directory raises IsADirectoryError."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("mydir")
        with pytest.raises(IsADirectoryError):
            fs.remove("mydir")

    def test_remove_many(self, tmp_path):
        """Test removing multiple files, leaving others intact."""
        fs = IsolatedFS(str(tmp_path))
        fs.write_many(
            {
                "a.txt": b"a",
                "b.txt": b"b",
                "c.txt": b"c",
            }
        )
        fs.remove_many(["a.txt", "b.txt"])
        assert fs.exists("a.txt") is False
        assert fs.exists("b.txt") is False
        assert fs.exists("c.txt") is True


# ---------------------------------------------------------------------------
# open()
# ---------------------------------------------------------------------------


class TestIsolatedOpen:
    """Test IsolatedFS.open() method."""

    def test_open_read_text(self, tmp_path):
        """Test opening a file for text reading."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"hello text")
        with fs.open("file.txt", "r") as f:
            content = f.read()
        assert content == "hello text"
        assert isinstance(content, str)

    def test_open_read_binary(self, tmp_path):
        """Test opening a file for binary reading."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.bin", b"\x00\x01\x02")
        with fs.open("file.bin", "rb") as f:
            content = f.read()
        assert content == b"\x00\x01\x02"
        assert isinstance(content, bytes)

    def test_open_write_text(self, tmp_path):
        """Test writing text via open('w')."""
        fs = IsolatedFS(str(tmp_path))
        with fs.open("file.txt", "w") as f:
            f.write("written via open")
        assert fs.read("file.txt") == b"written via open"

    def test_open_write_binary(self, tmp_path):
        """Test writing bytes via open('wb')."""
        fs = IsolatedFS(str(tmp_path))
        with fs.open("file.bin", "wb") as f:
            f.write(b"\xde\xad")
        assert fs.read("file.bin") == b"\xde\xad"

    def test_open_append(self, tmp_path):
        """Test appending via open('a')."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"first ")
        with fs.open("file.txt", "a") as f:
            f.write("second")
        assert fs.read("file.txt") == b"first second"

    def test_open_nonexistent_raises(self, tmp_path):
        """Test that opening a missing file for read raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.open("missing.txt", "r")

    def test_open_context_manager(self, tmp_path):
        """Test that open works as a context manager and auto-closes."""
        fs = IsolatedFS(str(tmp_path))
        with fs.open("ctx.txt", "w") as f:
            f.write("context")
        # File should be closed and content persisted
        assert fs.read("ctx.txt") == b"context"

    def test_open_write_missing_parent_raises(self, tmp_path):
        """Test that open in write mode raises if parent doesn't exist."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.open("a/b/c.txt", "w")


# ---------------------------------------------------------------------------
# Existence and type
# ---------------------------------------------------------------------------


class TestIsolatedExistsAndType:
    """Test exists/isfile/isdir."""

    def test_exists_file(self, tmp_path):
        """Test that exists returns True for files."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"x")
        assert fs.exists("file.txt") is True

    def test_exists_dir(self, tmp_path):
        """Test that exists returns True for directories."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("mydir")
        assert fs.exists("mydir") is True

    def test_exists_nonexistent(self, tmp_path):
        """Test that exists returns False for missing paths."""
        fs = IsolatedFS(str(tmp_path))
        assert fs.exists("nope") is False

    def test_isfile(self, tmp_path):
        """Test isfile: True for files, False for directories."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"x")
        fs.mkdir("adir")
        assert fs.isfile("file.txt") is True
        assert fs.isfile("adir") is False

    def test_isdir(self, tmp_path):
        """Test isdir: True for dirs, False for files."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("adir")
        fs.write("file.txt", b"x")
        assert fs.isdir("adir") is True
        assert fs.isdir("file.txt") is False

    def test_isdir_root(self, tmp_path):
        """Test that root directory is always a directory."""
        fs = IsolatedFS(str(tmp_path))
        assert fs.isdir("/") is True


# ---------------------------------------------------------------------------
# Directory operations
# ---------------------------------------------------------------------------


class TestIsolatedDirectoryOps:
    """Test mkdir, makedirs, rmdir."""

    def test_mkdir(self, tmp_path):
        """Test creating a single directory."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("newdir")
        assert fs.isdir("newdir") is True

    def test_mkdir_parents(self, tmp_path):
        """Test mkdir with parents=True creates the full tree."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("a/b/c", parents=True)
        assert fs.isdir("a") is True
        assert fs.isdir("a/b") is True
        assert fs.isdir("a/b/c") is True

    def test_mkdir_no_parents_raises(self, tmp_path):
        """Test mkdir with parents=False raises if parent missing."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.mkdir("x/y/z", parents=False)

    def test_mkdir_exist_ok_false_raises(self, tmp_path):
        """Test mkdir with exist_ok=False raises if directory exists."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("dup")
        with pytest.raises(FileExistsError):
            fs.mkdir("dup", exist_ok=False)

    def test_makedirs(self, tmp_path):
        """Test makedirs creates entire directory tree."""
        fs = IsolatedFS(str(tmp_path))
        fs.makedirs("a/b/c")
        assert fs.isdir("a") is True
        assert fs.isdir("a/b") is True
        assert fs.isdir("a/b/c") is True

    def test_rmdir(self, tmp_path):
        """Test removing an empty directory."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("empty")
        fs.rmdir("empty")
        assert fs.exists("empty") is False

    def test_rmdir_nonempty_raises(self, tmp_path):
        """Test that rmdir on a non-empty directory raises OSError."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("parent")
        fs.write("parent/child.txt", b"x")
        with pytest.raises(OSError):
            fs.rmdir("parent")

    def test_rmdir_nonexistent_raises(self, tmp_path):
        """Test that rmdir on a nonexistent path raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.rmdir("ghost")


# ---------------------------------------------------------------------------
# Rename / replace
# ---------------------------------------------------------------------------


class TestIsolatedRename:
    """Test rename and replace."""

    def test_rename_file(self, tmp_path):
        """Test renaming a file moves content to new name."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("old.txt", b"payload")
        fs.rename("old.txt", "new.txt")
        assert fs.exists("old.txt") is False
        assert fs.read("new.txt") == b"payload"

    def test_rename_directory(self, tmp_path):
        """Test renaming a directory."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("src")
        fs.write("src/file.txt", b"inside")
        fs.rename("src", "dst")
        assert fs.exists("src") is False
        assert fs.isdir("dst") is True
        assert fs.read("dst/file.txt") == b"inside"

    def test_rename_nonexistent_raises(self, tmp_path):
        """Test renaming a missing path raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.rename("nope", "also_nope")

    def test_replace_alias(self, tmp_path):
        """Test that replace() works the same as rename()."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("orig.txt", b"data")
        fs.replace("orig.txt", "moved.txt")
        assert fs.exists("orig.txt") is False
        assert fs.read("moved.txt") == b"data"


# ---------------------------------------------------------------------------
# listdir / list
# ---------------------------------------------------------------------------


class TestIsolatedListdir:
    """Test listdir and list."""

    def test_listdir_files(self, tmp_path):
        """Test listing files in root."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("a.txt", b"a")
        fs.write("b.txt", b"b")
        assert sorted(fs.list("/")) == ["a.txt", "b.txt"]

    def test_listdir_subdir(self, tmp_path):
        """Test listing files in a subdirectory."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("sub/x.txt", b"x")
        fs.write("sub/y.txt", b"y")
        assert sorted(fs.list("sub")) == ["x.txt", "y.txt"]

    def test_listdir_empty(self, tmp_path):
        """Test listing an empty directory returns empty list."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("empty")
        assert fs.list("empty") == []

    def test_listdir_recursive(self, tmp_path):
        """Test recursive listing."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("a.txt", b"a")
        fs.write("sub/b.txt", b"b")
        result = fs.list("/", recursive=True)
        assert "a.txt" in result
        assert "sub" in result
        assert os.path.join("sub", "b.txt") in result

    def test_list_nonexistent_raises(self, tmp_path):
        """Test listing a nonexistent directory raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.list("nope")

    def test_list_file_raises(self, tmp_path):
        """Test listing a file raises NotADirectoryError."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"data")
        with pytest.raises(NotADirectoryError):
            fs.list("file.txt")


# ---------------------------------------------------------------------------
# stat / getsize
# ---------------------------------------------------------------------------


class TestIsolatedStat:
    """Test stat and getsize."""

    def test_stat_file(self, tmp_path):
        """Test stat returns FileMetadata with correct size."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("data.bin", b"12345")
        meta = fs.stat("data.bin")
        assert isinstance(meta, FileMetadata)
        assert meta.size == 5

    def test_stat_directory(self, tmp_path):
        """Test stat returns is_dir=True for directories."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("subdir")
        meta = fs.stat("subdir")
        assert meta.is_dir is True
        assert meta.size == 0

    def test_stat_file_is_not_dir(self, tmp_path):
        """Test stat returns is_dir=False for files."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"data")
        meta = fs.stat("file.txt")
        assert meta.is_dir is False

    def test_stat_nonexistent_raises(self, tmp_path):
        """Test stat on missing path raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.stat("ghost.txt")

    def test_getsize(self, tmp_path):
        """Test getsize returns correct byte count."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("sized.txt", b"abcdefghij")
        assert fs.getsize("sized.txt") == 10


# ---------------------------------------------------------------------------
# CWD and paths
# ---------------------------------------------------------------------------


class TestIsolatedCwdAndPaths:
    """Test getcwd, chdir, resolve_path."""

    def test_getcwd_default(self, tmp_path):
        """Test default working directory is /."""
        fs = IsolatedFS(str(tmp_path))
        assert fs.getcwd() == "/"

    def test_chdir_and_getcwd(self, tmp_path):
        """Test chdir changes cwd and getcwd reflects it."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("work")
        fs.chdir("work")
        assert fs.getcwd() == "/work"

    def test_chdir_nonexistent_raises(self, tmp_path):
        """Test chdir to missing directory raises FileNotFoundError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.chdir("nowhere")

    def test_resolve_path_relative(self, tmp_path):
        """Test that relative paths resolve against cwd."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("sub")
        fs.chdir("sub")
        resolved = fs.resolve_path("file.txt")
        assert resolved == "/sub/file.txt"

    def test_resolve_path_absolute(self, tmp_path):
        """Test that absolute paths stay absolute."""
        fs = IsolatedFS(str(tmp_path))
        resolved = fs.resolve_path("/top/level.txt")
        assert resolved == "/top/level.txt"


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


class TestIsolatedGlob:
    """Test glob pattern matching."""

    def test_glob_star(self, tmp_path):
        """Test *.txt matches only .txt files."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("a.txt", b"a")
        fs.write("b.txt", b"b")
        fs.write("c.py", b"c")
        result = sorted(fs.glob("*.txt"))
        assert result == ["a.txt", "b.txt"]

    def test_glob_recursive(self, tmp_path):
        """Test **/*.py matches nested .py files."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("top.py", b"t")
        fs.write("pkg/mod.py", b"m")
        fs.write("pkg/sub/deep.py", b"d")
        result = sorted(fs.glob("**/*.py"))
        assert "pkg/mod.py" in result
        assert "pkg/sub/deep.py" in result

    def test_glob_absolute(self, tmp_path):
        """Test absolute glob pattern /dir/*.txt."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("dir/a.txt", b"a")
        fs.write("dir/b.txt", b"b")
        fs.write("dir/c.py", b"c")
        result = sorted(fs.glob("/dir/*.txt"))
        assert result == ["/dir/a.txt", "/dir/b.txt"]

    def test_glob_no_matches(self, tmp_path):
        """Test glob with no matches returns empty list."""
        fs = IsolatedFS(str(tmp_path))
        assert fs.glob("*.xyz") == []

    @pytest.mark.parametrize(
        "cwd, pattern, expected",
        [
            ("/workspace", "*.py", ["a.py"]),
            ("/workspace", "*", ["a.py", "s"]),
            ("/workspace", "s/*.py", ["s/b.py"]),
            ("/workspace", "**/*.py", ["a.py", "s/b.py", "s/t/c.py"]),
            ("/workspace", "*/**/*.py", ["s/b.py", "s/t/c.py"]),
            ("/workspace/s", "*.py", ["b.py"]),
            ("/workspace/s", "**/*.py", ["b.py", "t/c.py"]),
            ("/workspace", "*.txt", []),
        ],
    )
    def test_glob_relative_from_nested_cwd(self, tmp_path, cwd, pattern, expected):
        """A relative pattern searches the working directory, once (#39).

        The search root was the cwd stripped of its slash, which
        resolution joined onto the cwd again: from /workspace it searched
        /workspace/workspace and found nothing.
        """
        fs = IsolatedFS(str(tmp_path))
        fs.write("/top.py", b"")
        fs.write("/workspace/a.py", b"")
        fs.write("/workspace/s/b.py", b"")
        fs.write("/workspace/s/t/c.py", b"")
        fs.chdir(cwd)
        assert fs.glob(pattern) == expected
        # The absolute form of the same pattern finds the same paths.
        assert fs.glob(f"{cwd}/{pattern}") == [f"{cwd}/{p}" for p in expected]

    def test_glob_relative_pattern_can_climb_out_of_the_cwd(self, tmp_path):
        fs = IsolatedFS(str(tmp_path))
        fs.write("/workspace/a.py", b"")
        fs.write("/workspace/s/b.py", b"")
        fs.chdir("/workspace/s")
        assert fs.glob("../*.py") == ["../a.py"]

    def test_glob_double_star_from_nested_cwd_names_the_cwd_dot(self, tmp_path):
        """``**`` matches the directory it starts in, named ``.`` from any cwd."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("/workspace/s/b.py", b"")
        assert "." in fs.glob("**")
        fs.chdir("/workspace")
        result = fs.glob("**")
        assert "." in result
        assert "" not in result
        assert "s" in result

    @pytest.mark.parametrize("link", ["/link", "/re"])
    def test_glob_relative_from_a_symlinked_cwd(self, tmp_path, link):
        """Results stay relative to the cwd as it was named, a link included.

        The search starts from the cwd's resolved path, so relating each
        match back to the logical cwd by string missed (``/link`` against
        ``/real/a.py``) or cut a name short (``/re`` against ``/real/a.py``).
        """
        fs = IsolatedFS(str(tmp_path))
        fs.write("/real/a.py", b"")
        fs.write("/real/s/b.py", b"")
        fs.symlink("/real", link)
        fs.chdir(link)
        assert fs.getcwd() == link
        assert fs.glob("*.py") == ["a.py"]
        assert fs.glob("**/*.py") == ["a.py", "s/b.py"]
        assert fs.glob("*") == ["a.py", "s"]


# ---------------------------------------------------------------------------
# Staying inside the root: glob, listings and links
# ---------------------------------------------------------------------------

# Every name that would show the host outside the root: the root's
# sibling directory and what is in it, a file beside the root, and the
# root's own directory name.
_OUTSIDE_NAMES = ("OUTSIDE", "SECRET", "jail-root")


def _leaks(results):
    return [r for r in results if any(name in r for name in _OUTSIDE_NAMES)]


@pytest.fixture
def jail(tmp_path):
    """A root with links out of it, and recognisable names beside it.

    tmp_path/
      SIBLING-SECRET.txt
      OUTSIDE-dir/SECRET.txt
      jail-root/            <- the IsolatedFS root
        a/f.txt, a/b/
        a/b/up2 -> ../..    (the root itself: inside)
        inner -> a          (inside)
        out -> OUTSIDE-dir  (absolute, outside)
        up -> tmp_path      (absolute, the root's parent)
        dotdot -> ..        (relative, the root's parent)
        loop -> loop
    """
    host = tmp_path.resolve()
    (host / "SIBLING-SECRET.txt").write_text("sibling")
    outside = host / "OUTSIDE-dir"
    outside.mkdir()
    (outside / "SECRET.txt").write_text("secret")
    root = host / "jail-root"
    (root / "a" / "b").mkdir(parents=True)
    (root / "a" / "f.txt").write_text("f")
    (root / "a" / "b" / "up2").symlink_to("../..")
    (root / "inner").symlink_to("a")
    (root / "out").symlink_to(outside)
    (root / "up").symlink_to(host)
    (root / "dotdot").symlink_to("..")
    (root / "loop").symlink_to(root / "loop")
    return IsolatedFS(str(root))


_ROOT_NAMES = ["a", "dotdot", "inner", "loop", "out", "up"]


class TestIsolatedGlobStaysInsideRoot:
    """glob never names a path outside the root.

    ``..`` above the root is refused, as ``list()`` and every other method
    refuse it through ``_validate_path``, so a pattern that climbs there
    matches nothing. A link out of the root is listed, as ``list()`` lists
    it, but never searched, as ``list()`` refuses to list it.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            "/../*",
            "../*",
            "/..",
            "..",
            "/../..",
            "../..",
            "/../../*",
            "/a/../../*",
            "a/../../*",
            "/a/b/../../../*",
            "/../jail-root/*",
            "/../jail-root",
            "/out/*",
            "/out/**",
            "/up/*",
            "/up/*/*",
            "/up/jail-root/*",
            "/dotdot/*",
            "/dotdot/jail-root/a",
            "/a/b/up2/../*",
            "/inner/b/up2/..",
            "/loop/*",
            "/out/..",
            "/out/../*",
        ],
    )
    def test_a_pattern_out_of_the_root_matches_nothing(self, jail, pattern):
        assert jail.glob(pattern) == []

    @pytest.mark.parametrize(
        "pattern",
        [
            "/*/*",
            "*/*",
            "/*/*/*",
            "/*/../*",
            "/**/../*",
            "/**/*/..",
            "/*/**",
            "/**",
            "**",
            "/**/*",
            "/**/SECRET*",
            "/*/../../*",
            "/**/../../*",
            "*/../../*",
            "/*/../jail-root/*",
        ],
    )
    def test_wildcards_never_reach_out_of_the_root(self, jail, pattern):
        assert _leaks(jail.glob(pattern)) == []

    def test_a_link_out_of_the_root_is_listed_but_not_searched(self, jail):
        """As list() treats it: named among its siblings, never listed."""
        assert jail.glob("/*") == ["/" + name for name in jail.list("/")]
        assert jail.glob("/*") == ["/" + name for name in _ROOT_NAMES]
        assert jail.glob("/*/*") == [
            "/a/b",
            "/a/f.txt",
            "/inner/b",
            "/inner/f.txt",
        ]

    def test_dotdot_inside_the_root_still_matches(self, jail):
        assert jail.glob("/a/../*") == ["/a/../" + n for n in _ROOT_NAMES]
        assert jail.glob("/a/b/../f.txt") == ["/a/b/../f.txt"]
        # Through a link inside the root, .. is the link target's parent,
        # as the kernel and _validate_path resolve it.
        assert jail.glob("/inner/b/..") == ["/inner/b/.."]
        assert jail.glob("/*/../*") == sorted(
            f"/{d}/../{n}" for d in ("a", "inner") for n in _ROOT_NAMES
        )

    def test_a_link_back_into_the_root_is_searched(self, jail):
        assert jail.glob("/a/b/up2/*") == ["/a/b/up2/" + n for n in _ROOT_NAMES]
        assert jail.glob("/inner/*") == ["/inner/b", "/inner/f.txt"]

    def test_relative_patterns_from_a_nested_cwd(self, jail):
        jail.chdir("/a/b")
        assert jail.glob("../*") == ["../b", "../f.txt"]
        assert jail.glob("../../*") == ["../../" + n for n in _ROOT_NAMES]
        assert jail.glob("../../../*") == []
        assert jail.glob("../../..") == []
        assert jail.glob("../../out/*") == []
        assert jail.glob("up2/../*") == []
        assert _leaks(jail.glob("../../*/*")) == []

    def test_a_host_path_is_a_path_inside_the_root(self, jail, tmp_path):
        host = tmp_path.resolve()
        assert jail.glob(f"{host}/*") == []
        assert jail.glob(f"{host}/OUTSIDE-dir/*") == []


class TestIsolatedListingsStayInsideRoot:
    """Detailed listings report what stat() reports, and nothing outside."""

    def test_list_detailed_leaves_out_a_link_out_of_the_root(self, jail):
        """stat() refuses such a link, so it has no details to give.

        It used to report the target's: is_dir, size and times of a
        file outside the root.
        """
        listed = jail.list_detailed("/")
        assert [info.path for info in listed] == ["/a", "/inner"]
        listed = jail.list_detailed("/", recursive=True)
        assert _leaks([info.path for info in listed]) == []
        assert "/out" not in [info.path for info in listed]

    def test_list_detailed_leaves_out_a_dangling_link(self, jail):
        """It raised FileNotFoundError, naming the root's host path."""
        jail.symlink("/nowhere", "/dangling")
        assert "/dangling" not in [info.path for info in jail.list_detailed("/")]

    def test_metadata_snapshot_leaves_out_links_out_of_the_root(self, jail):
        jail.symlink("/nowhere", "/dangling")
        snapshot = jail.get_metadata_snapshot()
        assert sorted(snapshot) == sorted(
            ["a", "a/b", "a/f.txt", "inner", os.path.join("a", "b", "up2")]
        )


class TestIsolatedLinksStayInsideRoot:
    def test_readlink_refuses_an_absolute_target_climbing_out(self, jail, tmp_path):
        """Spelled from inside the root, but naming a path outside it."""
        root = tmp_path.resolve() / "jail-root"
        (root / "sneak").symlink_to(f"{root}/../OUTSIDE-dir")
        (root / "via").symlink_to(f"{root}/out/SECRET.txt")
        with pytest.raises(PermissionError, match="escapes sandbox"):
            jail.readlink("/sneak")
        with pytest.raises(PermissionError, match="escapes sandbox"):
            jail.readlink("/via")

    def test_lexists_and_islink_stop_at_the_root(self, jail):
        """``/..`` is the root's parent on the host, not a path inside."""
        assert jail.lexists("/..") is False
        assert jail.lexists("/a/b/up2/..") is False
        assert jail.islink("/..") is False

    def test_lexists_the_root_itself(self, jail):
        assert jail.lexists("/") is True
        assert jail.islink("/") is False


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


class TestIsolatedPathValidation:
    """Test that paths escaping root are rejected."""

    def test_path_escape_raises(self, tmp_path):
        """Test that ../secret raises PermissionError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(PermissionError):
            fs._validate_path("../secret")

    def test_dotdot_escape_raises(self, tmp_path):
        """Test that sub/../../secret raises PermissionError."""
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(PermissionError):
            fs._validate_path("sub/../../secret")


# ---------------------------------------------------------------------------
# list_detailed / listdir_detailed
# ---------------------------------------------------------------------------


class TestIsolatedListDetailed:
    """Test list_detailed and listdir_detailed."""

    def test_list_detailed(self, tmp_path):
        """Test list_detailed returns FileInfo objects."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("info.txt", b"info")
        result = fs.list_detailed("/")
        assert len(result) == 1
        item = result[0]
        assert isinstance(item, FileInfo)
        assert item.name == "info.txt"
        assert item.is_dir is False
        assert item.size == 4

    def test_list_detailed_recursive(self, tmp_path):
        """Test list_detailed with recursive=True."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("a.txt", b"a")
        fs.write("sub/b.txt", b"bb")
        result = fs.list_detailed("/", recursive=True)
        names = [fi.name for fi in result]
        assert "a.txt" in names
        assert "b.txt" in names
        assert "sub" in names


# ---------------------------------------------------------------------------
# Optional / OS-level methods
# ---------------------------------------------------------------------------


class TestIsolatedOptionalMethods:
    """Test optional methods that delegate to real OS calls."""

    def test_chmod(self, tmp_path):
        """Test chmod changes file permissions."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("script.sh", b"#!/bin/sh")
        fs.chmod("script.sh", 0o755)
        real_path = tmp_path / "script.sh"
        mode = os.stat(real_path).st_mode & 0o777
        assert mode == 0o755

    def test_access_readable(self, tmp_path):
        """Test access returns True for a readable file."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("read.txt", b"ok")
        assert fs.access("read.txt", os.R_OK) is True

    def test_access_nonexistent(self, tmp_path):
        """Test access returns False for missing file."""
        fs = IsolatedFS(str(tmp_path))
        assert fs.access("nope.txt", os.R_OK) is False

    def test_link_creates_hardlink(self, tmp_path):
        """Test link creates a hard link with the same inode."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("source.txt", b"shared")
        fs.link("source.txt", "linked.txt")
        assert fs.read("linked.txt") == b"shared"
        src_ino = os.stat(tmp_path / "source.txt").st_ino
        dst_ino = os.stat(tmp_path / "linked.txt").st_ino
        assert src_ino == dst_ino

    def test_truncate(self, tmp_path):
        """Test truncate shortens file to given length."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("data.txt", b"0123456789")
        fs.truncate("data.txt", 5)
        assert fs.getsize("data.txt") == 5
        assert fs.read("data.txt") == b"01234"

    def test_truncate_to_zero(self, tmp_path):
        """Test truncate to zero empties the file."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("data.txt", b"content")
        fs.truncate("data.txt", 0)
        assert fs.getsize("data.txt") == 0
        assert fs.read("data.txt") == b""

    def test_utime_sets_times(self, tmp_path):
        """utime() was the one dispatched method IsolatedFS never implemented."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("data.txt", b"content")
        fs.utime("data.txt", (1577836800.0, 1577836800.0))
        assert (tmp_path / "data.txt").stat().st_mtime == 1577836800.0

    def test_utime_none_uses_now(self, tmp_path):
        fs = IsolatedFS(str(tmp_path))
        fs.write("data.txt", b"content")
        os.utime(tmp_path / "data.txt", (1577836800.0, 1577836800.0))
        fs.utime("data.txt", None)
        assert (tmp_path / "data.txt").stat().st_mtime > 1577836800.0

    def test_utime_missing_file_raises(self, tmp_path):
        fs = IsolatedFS(str(tmp_path))
        with pytest.raises(FileNotFoundError):
            fs.utime("missing.txt", None)

    def test_remove_symlink_removes_the_link_not_the_target(self, tmp_path):
        """unlink() never follows the final component."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("target.txt", b"target data")
        fs.symlink("target.txt", "link.txt")
        fs.remove("link.txt")
        assert (tmp_path / "target.txt").is_file(), "remove() deleted the link target"
        assert (tmp_path / "target.txt").read_bytes() == b"target data"
        assert not (tmp_path / "link.txt").is_symlink(), "the link itself survived"

    def test_symlink_and_readlink(self, tmp_path):
        """Test symlink creation and readlink."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("target.txt", b"target data")
        fs.symlink("target.txt", "link.txt")
        assert fs.islink("link.txt")
        target_str = fs.readlink("link.txt")
        assert "target.txt" in target_str

    def test_readlink_answers_in_the_virtual_namespace(self, tmp_path):
        """The host root must not leak out through a link target.

        symlink() stores the host path the kernel needs; readlink() is
        its inverse and reports the same target as the caller named it.
        A host path here would name the sandbox's real location and
        would not route through anything composing this filesystem.
        """
        fs = IsolatedFS(str(tmp_path))
        fs.write("target.txt", b"target data")
        fs.symlink("target.txt", "link.txt")

        assert fs.readlink("link.txt") == "/target.txt"
        assert str(tmp_path) not in fs.readlink("link.txt")

    def test_readlink_target_round_trips_through_symlink(self, tmp_path):
        """What readlink() returns, symlink() takes back."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("dir/target.txt", b"data")
        fs.symlink("dir/target.txt", "first.txt")

        fs.symlink(fs.readlink("first.txt"), "second.txt")

        assert fs.readlink("second.txt") == fs.readlink("first.txt")
        assert fs.read("second.txt") == b"data"

    def test_readlink_keeps_a_relative_target_relative(self, tmp_path):
        """A relative target names the same file in either namespace."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("dir/target.txt", b"data")
        (tmp_path / "dir" / "link.txt").symlink_to("target.txt")

        assert fs.readlink("dir/link.txt") == "target.txt"

    def test_readlink_blocks_escaping_relative_target(self, tmp_path):
        """Test readlink rejects relative symlinks that escape the sandbox."""
        fs = IsolatedFS(str(tmp_path))
        # Create an escaping relative symlink directly on the real FS
        link_path = tmp_path / "escape.txt"
        link_path.symlink_to("../../etc/passwd")
        with pytest.raises(PermissionError, match="escapes sandbox"):
            fs.readlink("escape.txt")

    def test_symlink_and_islink(self, tmp_path):
        """Test islink returns True for symbolic links."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("real.txt", b"data")
        fs.symlink("real.txt", "sym.txt")
        assert fs.islink("sym.txt") is True

    def test_islink_regular_file(self, tmp_path):
        """Test islink returns False for regular files."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("plain.txt", b"x")
        assert fs.islink("plain.txt") is False

    def test_islink_cwd_relative(self, tmp_path):
        """Test islink resolves relative paths against CWD."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("subdir")
        fs.write("subdir/target.txt", b"data")
        fs.symlink("subdir/target.txt", "subdir/link.txt")
        fs.chdir("subdir")
        assert fs.islink("link.txt") is True
        assert fs.islink("target.txt") is False

    def test_lexists(self, tmp_path):
        """Test lexists: True for existing file, False for missing."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("here.txt", b"x")
        assert fs.lexists("here.txt") is True
        assert fs.lexists("not_here.txt") is False

    def test_samefile_same(self, tmp_path):
        """Test samefile returns True for the same path."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"x")
        assert fs.samefile("file.txt", "file.txt") is True

    def test_samefile_different(self, tmp_path):
        """Test samefile returns False for different files."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("one.txt", b"1")
        fs.write("two.txt", b"2")
        assert fs.samefile("one.txt", "two.txt") is False

    def test_realpath(self, tmp_path):
        """Test realpath returns canonical virtual path."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("file.txt", b"x")
        rp = fs.realpath("file.txt")
        assert rp == "/file.txt"

    def test_get_metadata_snapshot(self, tmp_path):
        """Test get_metadata_snapshot returns tracked entries."""
        fs = IsolatedFS(str(tmp_path))
        fs.write("a.txt", b"aaa")
        fs.write("b.txt", b"bb")
        snapshot = fs.get_metadata_snapshot()
        assert "a.txt" in snapshot
        assert "b.txt" in snapshot
        assert all(isinstance(v, FileMetadata) for v in snapshot.values())

    def test_get_metadata_snapshot_includes_directories(self, tmp_path):
        """Test get_metadata_snapshot includes directories with is_dir=True."""
        fs = IsolatedFS(str(tmp_path))
        fs.mkdir("subdir")
        fs.write("subdir/file.txt", b"data")
        snapshot = fs.get_metadata_snapshot()
        assert "subdir" in snapshot
        assert snapshot["subdir"].is_dir is True
        assert snapshot["subdir"].size == 0
        assert os.path.join("subdir", "file.txt") in snapshot
        assert snapshot[os.path.join("subdir", "file.txt")].is_dir is False
