"""A path is a file or a directory, never both (#34).

``VirtualFS`` used to take a write over a directory, so a path could answer
yes to both ``isfile()`` and ``isdir()``, and a file could hold children.
A real filesystem refuses both: a file over a directory is EISDIR, and
anything below a file is ENOTDIR. ``IsolatedFS`` gets those answers from the
OS, so most tests here run the same calls on both backends and require the
same outcome.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from monkeyfs import IsolatedFS, MountFS, VirtualFS, check_filesystem, patch

from .test_open_optional import TermishFS


@pytest.fixture(params=["virtual", "isolated"])
def fs(request, tmp_path):
    if request.param == "virtual":
        return VirtualFS({})
    return IsolatedFS(root=str(tmp_path))


def _tree(fs) -> list[str]:
    return fs.list("/", recursive=True)


def _outcome(call) -> tuple[type[BaseException] | None, int | None]:
    try:
        call()
    except OSError as error:
        return type(error), error.errno
    return None, None


class TestTheIssueRepros:
    """The cases from #34, each of which VirtualFS used to accept."""

    def test_a_write_over_an_empty_directory(self, fs):
        fs.makedirs("x")
        with pytest.raises(IsADirectoryError):
            fs.write("x", b"f")
        assert fs.isdir("x") and not fs.isfile("x")

    def test_a_write_over_a_directory_with_a_subdirectory(self, fs):
        fs.makedirs("x/sub")
        with pytest.raises(IsADirectoryError):
            fs.write("x", b"f")
        assert not fs.isfile("x")
        assert _tree(fs) == ["x", "x/sub"]

    def test_a_write_over_a_directory_holding_a_file(self, fs):
        fs.makedirs("x")
        fs.write("x/f", b"1")
        with pytest.raises(IsADirectoryError):
            fs.write("x", b"f")
        assert not fs.isfile("x") and fs.read("x/f") == b"1"

    def test_write_many_over_a_directory(self, fs):
        fs.makedirs("x")
        fs.write("x/f", b"1")
        with pytest.raises(IsADirectoryError):
            fs.write_many({"x": b"f"})
        assert not fs.isfile("x")

    def test_rename_of_a_file_over_a_directory(self, fs):
        fs.makedirs("x")
        fs.write("x/f", b"1")
        fs.write("y", b"y")
        with pytest.raises(IsADirectoryError):
            fs.rename("y", "x")
        assert fs.read("y") == b"y" and not fs.isfile("x")


class TestWriteOverADirectory:
    def test_append_mode(self, fs):
        fs.makedirs("x")
        with pytest.raises(IsADirectoryError):
            fs.write("x", b"f", mode="a")
        assert not fs.isfile("x")

    def test_the_error_carries_eisdir(self, fs):
        fs.makedirs("x")
        with pytest.raises(OSError) as caught:
            fs.write("x", b"f")
        assert caught.value.errno == errno.EISDIR

    def test_root(self):
        with pytest.raises(IsADirectoryError):
            VirtualFS({}).write("/", b"f")

    def test_a_directory_named_relative_to_a_working_directory(self, fs):
        fs.makedirs("/work/x")
        fs.chdir("/work")
        with pytest.raises(IsADirectoryError):
            fs.write("x", b"f")
        with pytest.raises(IsADirectoryError):
            fs.write("./x/", b"f")
        assert fs.isdir("/work/x") and not fs.isfile("/work/x")

    def test_a_directory_there_only_because_a_file_is_in_it(self):
        """An implicit directory (no row of its own) is a directory too."""
        vfs = VirtualFS({})
        vfs.write("x/f", b"1")
        with pytest.raises(IsADirectoryError):
            vfs.write("x", b"f")

    def test_write_many_checks_every_path_before_writing_any(self):
        """VirtualFS writes a batch all or nothing, whatever the order."""
        vfs = VirtualFS({})
        vfs.makedirs("x")
        for batch in ({"a.txt": b"a", "x": b"f"}, {"x": b"f", "a.txt": b"a"}):
            with pytest.raises(IsADirectoryError):
                vfs.write_many(batch)
            assert not vfs.exists("a.txt")
            assert _tree(vfs) == ["x"]


class TestWriteBelowAFile:
    """The counterpart: termish's MemoryFS made a file a directory as well
    when a path was written below it (termish#31). VirtualFS refused it,
    but with FileExistsError, and ``write_many`` took it."""

    @pytest.mark.parametrize("below", ["f/child", "f/a/child"])
    def test_write(self, fs, below):
        fs.write("f", b"1")
        with pytest.raises(NotADirectoryError):
            fs.write(below, b"c")
        assert fs.isfile("f") and not fs.isdir("f") and _tree(fs) == ["f"]

    def test_write_many(self, fs):
        fs.write("f", b"1")
        with pytest.raises(NotADirectoryError):
            fs.write_many({"f/child": b"c"})
        assert fs.isfile("f") and not fs.isdir("f") and _tree(fs) == ["f"]

    @pytest.mark.parametrize(
        "batch",
        [
            {"g": b"file", "g/child": b"c"},
            {"g/child": b"c", "g": b"file"},
            {"g/a/child": b"c", "g": b"file", "other": b"o"},
        ],
    )
    def test_write_many_below_a_file_it_writes_itself(self, batch):
        vfs = VirtualFS({})
        with pytest.raises(NotADirectoryError):
            vfs.write_many(batch)
        assert _tree(vfs) == []

    @pytest.mark.parametrize("below", ["f/child", "f/a/child"])
    def test_mkdir_and_makedirs(self, fs, below):
        fs.write("f", b"1")
        for call in (
            lambda: fs.mkdir(below),
            lambda: fs.mkdir(below, parents=True),
            lambda: fs.makedirs(below),
        ):
            with pytest.raises(NotADirectoryError):
                call()
        assert _tree(fs) == ["f"]

    def test_makedirs_on_the_file_itself_is_still_eexist(self, fs):
        fs.write("f", b"1")
        with pytest.raises(FileExistsError):
            fs.makedirs("f")
        with pytest.raises(FileExistsError):
            fs.mkdir("f")

    def test_a_missing_directory_is_still_enoent(self, fs):
        with pytest.raises(FileNotFoundError):
            fs.mkdir("missing/child")


class TestOpen:
    @pytest.mark.parametrize("mode", ["w", "wb", "a", "ab", "r+", "rb+"])
    def test_a_directory_opened_to_write(self, fs, mode):
        fs.makedirs("x")
        with pytest.raises(IsADirectoryError):
            fs.open("x", mode)
        assert fs.isdir("x") and not fs.isfile("x")

    def test_exclusive_creation_over_a_directory_is_eexist(self, fs):
        fs.makedirs("x")
        with pytest.raises(FileExistsError):
            fs.open("x", "x")

    @pytest.mark.parametrize("below", ["f/child", "f/a/child"])
    def test_a_file_opened_below_a_file(self, fs, below):
        fs.write("f", b"1")
        with pytest.raises(NotADirectoryError):
            fs.open(below, "w")
        assert _tree(fs) == ["f"]

    def test_a_missing_parent_is_still_enoent(self, fs):
        with pytest.raises(FileNotFoundError):
            fs.open("missing/child", "w")

    def test_the_synthesized_open_of_a_backend_without_one(self):
        """``open_file`` is shared: a backend with no open() of its own
        gets the same answers under ``patch()``."""
        backend = TermishFS()
        backend.write("/x/f", b"1")
        backend.write("/g", b"1")
        with patch(backend):
            with pytest.raises(IsADirectoryError):
                open("/x", "w")
            with pytest.raises(NotADirectoryError):
                open("/g/child", "w")

    def test_under_patch(self):
        vfs = VirtualFS({})
        vfs.makedirs("/x")
        vfs.write("/f", b"1")
        with patch(vfs):
            with pytest.raises(IsADirectoryError):
                open("/x", "w")
            with pytest.raises(IsADirectoryError):
                Path("/x").write_text("f")
            with pytest.raises(NotADirectoryError):
                open("/f/child", "w")
            with pytest.raises(NotADirectoryError):
                os.makedirs("/f/child")
        assert vfs.isdir("/x") and not vfs.isfile("/x")
        assert _tree(vfs) == ["f", "x"]

    @pytest.mark.parametrize(
        "flags",
        [
            os.O_WRONLY,
            os.O_RDWR,
            os.O_WRONLY | os.O_CREAT,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            os.O_WRONLY | os.O_APPEND,
        ],
    )
    def test_os_open_of_a_directory_to_write(self, flags):
        """EISDIR at open(2), not at the flush on close()."""
        vfs = VirtualFS({})
        vfs.makedirs("/x")
        with patch(vfs):
            with pytest.raises(IsADirectoryError):
                os.open("/x", flags)
        assert vfs.isdir("/x") and not vfs.isfile("/x")

    def test_os_open_exclusive_over_a_directory_is_eexist(self):
        vfs = VirtualFS({})
        vfs.makedirs("/x")
        with patch(vfs):
            with pytest.raises(FileExistsError):
                os.open("/x", os.O_WRONLY | os.O_CREAT | os.O_EXCL)


# What rename(2) does with each shape of source and destination. Each case
# runs on both backends; VirtualFS must end where IsolatedFS (os.rename on
# this platform) ends, error and tree alike.
RENAMES = {
    "file onto an empty directory": (
        lambda fs: (fs.write("y", b"y"), fs.makedirs("x")),
        "y",
        "x",
    ),
    "file onto a directory holding a file": (
        lambda fs: (fs.write("y", b"y"), fs.write("x/f", b"1")),
        "y",
        "x",
    ),
    "file onto a file": (
        lambda fs: (fs.write("y", b"y"), fs.write("x", b"1")),
        "y",
        "x",
    ),
    "file onto itself": (lambda fs: fs.write("y", b"y"), "y", "y"),
    "file onto itself, spelled differently": (
        lambda fs: fs.write("d/y", b"y"),
        "d/y",
        "d/../d/./y",
    ),
    "file below a file": (
        lambda fs: (fs.write("y", b"y"), fs.write("f", b"1")),
        "y",
        "f/z",
    ),
    "file onto its own directory": (lambda fs: fs.write("a/f", b"a"), "a/f", "a"),
    "directory onto a file": (
        lambda fs: (fs.write("a/f", b"a"), fs.write("x", b"1")),
        "a",
        "x",
    ),
    "directory onto an empty directory": (
        lambda fs: (fs.write("a/f", b"a"), fs.makedirs("x")),
        "a",
        "x",
    ),
    "empty directory onto an empty directory": (
        lambda fs: (fs.makedirs("a"), fs.makedirs("x")),
        "a",
        "x",
    ),
    "directory onto a directory holding a file": (
        lambda fs: (fs.write("a/f", b"a"), fs.write("x/g", b"1")),
        "a",
        "x",
    ),
    "directory onto a directory holding an empty directory": (
        lambda fs: (fs.write("a/f", b"a"), fs.makedirs("x/s")),
        "a",
        "x",
    ),
    "directory onto itself": (lambda fs: fs.write("a/f", b"a"), "a", "a"),
    "directory into itself": (lambda fs: fs.write("a/f", b"a"), "a", "a/b"),
    "directory deep into itself": (lambda fs: fs.write("a/s/f", b"a"), "a", "a/s/b"),
    "directory onto its parent": (lambda fs: fs.write("a/b/f", b"a"), "a/b", "a"),
    "directory below a file": (
        lambda fs: (fs.write("a/f", b"a"), fs.write("f", b"1")),
        "a",
        "f/z",
    ),
    "directory onto a sibling sharing its prefix": (
        lambda fs: fs.write("a/f", b"a"),
        "a",
        "ab",
    ),
    "missing source": (lambda fs: None, "nope", "x"),
}


# Where the host kernels themselves disagree, VirtualFS gives one answer on
# every platform, and IsolatedFS (os.rename) may give any of the hosts'.
# Each entry: VirtualFS's answer, and every answer a host gives.
HOST_DIVERGENCES = {
    # rename(2) says a file onto a directory is EISDIR, and macOS answers
    # that. Linux checks the directory for emptiness first, and this one
    # holds the file being moved, so it answers ENOTEMPTY. VirtualFS keeps
    # EISDIR: it is the answer POSIX names, and the one every other file
    # onto a directory gets on both hosts.
    "file onto its own directory": (
        (IsADirectoryError, errno.EISDIR),
        {(IsADirectoryError, errno.EISDIR), (OSError, errno.ENOTEMPTY)},
    ),
}


class TestRenameMatchesIsolatedFS:
    @pytest.mark.parametrize("case", list(RENAMES))
    def test_rename(self, case, tmp_path):
        setup, src, dst = RENAMES[case]
        results = []
        for fs in (VirtualFS({}), IsolatedFS(root=str(tmp_path))):
            setup(fs)
            raised, code = _outcome(lambda: fs.rename(src, dst))
            files = {name: fs.read(name) for name in _tree(fs) if fs.isfile(name)}
            results.append((raised, code, _tree(fs), files))
        virtual, isolated = results
        if case in HOST_DIVERGENCES:
            chosen, hosts = HOST_DIVERGENCES[case]
            assert virtual[:2] == chosen, (case, virtual)
            assert isolated[:2] in hosts, (case, isolated)
        else:
            assert virtual[0] is isolated[0], (case, virtual, isolated)
            if virtual[1] is not None:
                assert virtual[1] == isolated[1], (case, virtual, isolated)
        assert virtual[2:] == isolated[2:], (case, virtual, isolated)

    def test_every_divergence_names_a_case(self):
        assert set(HOST_DIVERGENCES) <= set(RENAMES)

    def test_the_errors_rename_2_names(self):
        """Checked by errno as well: OSError carries ENOTEMPTY and EINVAL."""
        vfs = VirtualFS({})
        vfs.write("a/f", b"a")
        vfs.write("x/g", b"1")
        assert _outcome(lambda: vfs.rename("a", "x")) == (OSError, errno.ENOTEMPTY)
        assert _outcome(lambda: vfs.rename("a", "a/b")) == (OSError, errno.EINVAL)
        assert _outcome(lambda: vfs.rename("a", "x/g")) == (
            NotADirectoryError,
            errno.ENOTDIR,
        )
        assert _outcome(lambda: vfs.rename("x/g", "a")) == (
            IsADirectoryError,
            errno.EISDIR,
        )

    def test_a_file_renamed_onto_itself_keeps_its_content(self):
        """It used to be written to itself and then removed."""
        vfs = VirtualFS({})
        vfs.write("y", b"keep")
        vfs.rename("y", "y")
        vfs.replace("y", "/y")
        assert vfs.read("y") == b"keep"

    def test_an_empty_directory_replaced_by_one_with_a_row(self):
        vfs = VirtualFS({})
        vfs.makedirs("a/inner")
        vfs.makedirs("x")
        vfs.rename("a", "x")
        assert vfs.isdir("x/inner") and not vfs.exists("a")
        assert _tree(vfs) == ["x", "x/inner"]


class TestMountFSImpliedDirectories:
    """A directory MountFS shows on the way to a mount may be held by no
    filesystem, so the base would take a file there. The composition
    refuses it, as a directory a filesystem holds refuses it."""

    @pytest.fixture
    def composed(self):
        base = VirtualFS({})
        docs = VirtualFS({})
        docs.write("readme.md", b"# docs")
        return base, MountFS(base, {"/workspace/docs": docs})

    def test_write(self, composed):
        base, fs = composed
        with pytest.raises(IsADirectoryError):
            fs.write("/workspace", b"f")
        with pytest.raises(IsADirectoryError):
            fs.write_many({"/a.txt": b"a", "/workspace": b"f"})
        assert _tree(base) == []
        assert fs.isdir("/workspace") and not fs.isfile("/workspace")

    def test_the_mount_point_itself(self, composed):
        _, fs = composed
        with pytest.raises(IsADirectoryError):
            fs.write("/workspace/docs", b"f")

    @pytest.mark.parametrize("mode", ["w", "a", "r+"])
    def test_open(self, composed, mode):
        base, fs = composed
        with pytest.raises(IsADirectoryError):
            fs.open("/workspace", mode)
        with pytest.raises(FileExistsError):
            fs.open("/workspace", "x")
        assert _tree(base) == []

    def test_rename_and_replace(self, composed):
        base, fs = composed
        fs.write("/y", b"y")
        fs.makedirs("/d")
        for move in (fs.rename, fs.replace):
            assert _outcome(lambda: move("/y", "/workspace")) == (
                IsADirectoryError,
                errno.EISDIR,
            )
            assert _outcome(lambda: move("/d", "/workspace")) == (
                OSError,
                errno.ENOTEMPTY,
            )
        assert _tree(base) == ["d", "y"]

    @pytest.mark.parametrize("how", ["rename", "replace"])
    def test_a_missing_source_is_enoent_before_the_destination(self, composed, how):
        base, fs = composed
        move = getattr(fs, how)
        assert _outcome(lambda: move("/missing", "/workspace"))[0] is (
            FileNotFoundError
        )
        assert _tree(base) == []

    @pytest.mark.parametrize("how", ["rename", "replace"])
    def test_an_implied_directory_onto_itself_changes_nothing(self, composed, how):
        base, fs = composed
        move = getattr(fs, how)
        move("/workspace", "/workspace")
        move("/workspace", "/workspace/")
        assert fs.isdir("/workspace") and fs.read("/workspace/docs/readme.md")
        assert _tree(base) == []

    def test_under_patch(self, composed):
        base, fs = composed
        with patch(fs):
            with pytest.raises(IsADirectoryError):
                open("/workspace", "w")
        assert _tree(base) == []


class TestIsolatedFS:
    def test_a_write_just_below_a_file_is_enotdir(self, tmp_path):
        """Path.mkdir(exist_ok=True) on the file reported EEXIST, where
        open(2) reports ENOTDIR; deeper below the file it already did."""
        fs = IsolatedFS(root=str(tmp_path))
        fs.write("f", b"1")
        with pytest.raises(NotADirectoryError) as caught:
            fs.write("f/child", b"c")
        assert caught.value.errno == errno.ENOTDIR
        assert caught.value.filename == "f/child"


class TestTheConformanceKit:
    def test_a_write_over_a_directory_fails_it(self):
        class OverDirectories(VirtualFS):
            def write(self, path, content, mode="w"):
                if self.isdir(path):
                    self._state[self._encode_path(path)] = content
                    return
                super().write(path, content, mode)

        with pytest.raises(AssertionError, match="IsADirectoryError"):
            check_filesystem(OverDirectories({}))

    def test_a_write_below_a_file_fails_it(self):
        class BelowFiles(VirtualFS):
            def _file_on_the_way(self, normalized):
                return None

        with pytest.raises(AssertionError, match="NotADirectoryError"):
            check_filesystem(BelowFiles({}))

    def test_a_refusal_with_the_wrong_exception_fails_it(self):
        class WrongError(VirtualFS):
            def write(self, path, content, mode="w"):
                if self.isdir(path):
                    raise PermissionError(path)
                super().write(path, content, mode)

        with pytest.raises(AssertionError, match="not PermissionError"):
            check_filesystem(WrongError({}))
