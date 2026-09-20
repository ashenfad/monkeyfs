"""``open()`` is what monkeyfs provides, not what it demands.

A backend that speaks only bytes -- the shape termish's ``FileSystem``
protocol describes, with ``read``/``write``/``stat`` and no file objects at
all -- must be patchable, and ``builtins.open()`` must work over it in every
mode. A backend that has a better ``open()`` of its own must keep being asked
for it, because a real file descriptor is a thing no synthesized object can
produce.
"""

from __future__ import annotations

import io
import mmap
import os
import posixpath

import pytest

from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS, VirtualFS, patch
from monkeyfs.base import FileInfo, FileMetadata, FileSystem
from monkeyfs.virtualfile import BLOCK_SIZE, LazyBinaryFile

STAMP = "2026-01-01T00:00:00+00:00"

#: The protocol a termish-shaped filesystem implements, and all of it.
TERMISH_METHODS = {
    "chdir",
    "exists",
    "getcwd",
    "glob",
    "isdir",
    "isfile",
    "list",
    "list_detailed",
    "makedirs",
    "mkdir",
    "read",
    "remove",
    "rename",
    "rmdir",
    "stat",
    "write",
}


class TermishFS:
    """A filesystem with the sixteen bytes-level methods and nothing else.

    Deliberately written from scratch rather than derived from VirtualFS: the
    claim under test is that a backend which has never heard of monkeyfs's
    file objects still works under ``patch()``, and inheriting one would be
    inheriting the thing being claimed unnecessary.
    """

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}
        self._dirs: set[str] = {"/"}
        self._cwd = "/"
        self.reads: list[tuple[int, int]] = []
        self.bytes_read = 0

    def _abs(self, path: str) -> str:
        if not path.startswith("/"):
            path = posixpath.join(self._cwd, path)
        return posixpath.normpath(path)

    def reset_counts(self) -> None:
        self.reads.clear()
        self.bytes_read = 0

    # -- the protocol --

    def getcwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        target = self._abs(path)
        if not self.isdir(target):
            raise FileNotFoundError(path)
        self._cwd = target

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        if offset < 0:
            raise ValueError(f"negative read offset: {offset}")
        target = self._abs(path)
        if target not in self._files:
            raise FileNotFoundError(path)
        content = self._files[target]
        chunk = content[offset:] if size < 0 else content[offset : offset + size]
        self.reads.append((offset, size))
        self.bytes_read += len(chunk)
        return chunk

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        target = self._abs(path)
        if mode == "a":
            content = self._files.get(target, b"") + content
        self._files[target] = content
        parent = posixpath.dirname(target)
        while parent and parent != "/":
            self._dirs.add(parent)
            parent = posixpath.dirname(parent)

    def exists(self, path: str) -> bool:
        return self.isfile(path) or self.isdir(path)

    def isfile(self, path: str) -> bool:
        return self._abs(path) in self._files

    def isdir(self, path: str) -> bool:
        target = self._abs(path)
        if target in self._dirs:
            return True
        prefix = target.rstrip("/") + "/"
        return any(name.startswith(prefix) for name in self._files)

    def stat(self, path: str) -> FileMetadata:
        target = self._abs(path)
        if target in self._files:
            return FileMetadata(
                size=len(self._files[target]),
                created_at=STAMP,
                modified_at=STAMP,
            )
        if self.isdir(target):
            return FileMetadata(
                size=0, created_at=STAMP, modified_at=STAMP, is_dir=True
            )
        raise FileNotFoundError(path)

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        target = self._abs(path)
        if self.exists(target) and not exist_ok:
            raise FileExistsError(path)
        parent = posixpath.dirname(target)
        if not parents and parent and not self.isdir(parent):
            raise FileNotFoundError(path)
        self._dirs.add(target)

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        target = self._abs(path)
        if target in self._dirs and not exist_ok:
            raise FileExistsError(path)
        built = ""
        for part in target.strip("/").split("/"):
            built += "/" + part
            self._dirs.add(built)

    def remove(self, path: str) -> None:
        target = self._abs(path)
        if target not in self._files:
            raise FileNotFoundError(path)
        del self._files[target]

    def rmdir(self, path: str) -> None:
        target = self._abs(path)
        if self.list(target):
            raise OSError(f"Directory not empty: '{path}'")
        self._dirs.discard(target)

    def rename(self, src: str, dst: str) -> None:
        source = self._abs(src)
        if source in self._files:
            self._files[self._abs(dst)] = self._files.pop(source)
            return
        raise FileNotFoundError(src)

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        target = self._abs(path)
        prefix = "/" if target == "/" else target + "/"
        names = set()
        for candidate in list(self._files) + list(self._dirs):
            if candidate == target or not candidate.startswith(prefix):
                continue
            remainder = candidate[len(prefix) :]
            names.add(remainder if recursive else remainder.split("/")[0])
        return sorted(names)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[FileInfo]:
        entries = []
        base = self._abs(path)
        for name in self.list(path, recursive):
            full = posixpath.join(base, name)
            meta = self.stat(full)
            entries.append(
                FileInfo(
                    name=name,
                    path=full,
                    size=meta.size,
                    created_at=meta.created_at,
                    modified_at=meta.modified_at,
                    is_dir=meta.is_dir,
                )
            )
        return entries

    def glob(self, pattern: str) -> list[str]:
        import fnmatch

        return sorted(
            name for name in self._files if fnmatch.fnmatch(name, self._abs(pattern))
        )


@pytest.fixture
def termish() -> TermishFS:
    return TermishFS()


class TestTheFixtureItself:
    """The claim is only worth anything if the backend really is that small."""

    def test_it_has_the_sixteen_methods_and_no_others(self, termish):
        public = {
            name
            for name in dir(termish)
            if not name.startswith("_") and callable(getattr(termish, name))
        }
        assert public - {"reset_counts"} == TERMISH_METHODS

    def test_it_has_no_open_and_no_readlink(self, termish):
        assert not hasattr(termish, "open")
        assert not hasattr(termish, "readlink")

    def test_it_satisfies_the_protocol(self, termish):
        assert isinstance(termish, FileSystem)


class TestSynthesizedOpen:
    """builtins.open() over a backend that has none."""

    def test_write_and_read_text(self, termish):
        with patch(termish):
            with open("notes.txt", "w") as handle:
                handle.write("hello\nthere\n")

            assert open("notes.txt").read() == "hello\nthere\n"
            with open("notes.txt") as handle:
                assert handle.readlines() == ["hello\n", "there\n"]

    def test_write_and_read_binary(self, termish):
        with patch(termish):
            with open("data.bin", "wb") as handle:
                handle.write(b"\x00\x01\x02")

            with open("data.bin", "rb") as handle:
                assert handle.read() == b"\x00\x01\x02"

    def test_append(self, termish):
        with patch(termish):
            with open("log.txt", "w") as handle:
                handle.write("one\n")
            with open("log.txt", "a") as handle:
                handle.write("two\n")
            with open("log.bin", "ab") as handle:
                handle.write(b"first")

            assert open("log.txt").read() == "one\ntwo\n"
            assert open("log.bin", "rb").read() == b"first"

    def test_update_mode_round_trips(self, termish):
        with patch(termish):
            with open("x.txt", "w") as handle:
                handle.write("abcdef")
            with open("x.txt", "r+") as handle:
                assert handle.read(3) == "abc"
                handle.write("XYZ")

            assert open("x.txt").read() == "abcXYZ"

    def test_exclusive_creation(self, termish):
        with patch(termish):
            with open("once.txt", "x") as handle:
                handle.write("first")
            with pytest.raises(FileExistsError):
                open("once.txt", "x")

    def test_missing_file_raises(self, termish):
        with patch(termish):
            with pytest.raises(FileNotFoundError):
                open("nope.txt")
            with pytest.raises(FileNotFoundError):
                open("nope.txt", "rb")

    def test_a_missing_parent_is_enoent_not_a_new_tree(self, termish):
        with patch(termish):
            with pytest.raises(FileNotFoundError):
                open("/nowhere/file.txt", "w")

            os.makedirs("/somewhere")
            with open("/somewhere/file.txt", "w") as handle:
                handle.write("fine")

            assert os.path.exists("/somewhere/file.txt")

    def test_a_relative_parent_is_checked_from_the_cwd(self, termish):
        # A backend without resolve_path still has getcwd, and a relative
        # path means "from here": with the cwd at /work, `sub/file.txt`
        # must be judged by /work/sub, not by /sub.
        with patch(termish):
            os.makedirs("/work/sub")
            os.makedirs("/other")
            os.chdir("/work")

            with open("sub/file.txt", "w") as handle:
                handle.write("here")
            assert os.path.exists("/work/sub/file.txt")
            assert not os.path.exists("/sub/file.txt")

            # The mirror case: a parent that exists only at the root must
            # not make a missing one under the cwd look present.
            with pytest.raises(FileNotFoundError):
                open("other/file.txt", "w")

    def test_the_rest_of_the_stdlib_still_routes(self, termish):
        with patch(termish):
            os.makedirs("/work/sub")
            with open("/work/sub/a.txt", "w") as handle:
                handle.write("payload")

            assert os.path.isfile("/work/sub/a.txt")
            assert os.path.isdir("/work/sub")
            assert os.listdir("/work/sub") == ["a.txt"]
            assert os.stat("/work/sub/a.txt").st_size == 7
            os.rename("/work/sub/a.txt", "/work/sub/b.txt")
            assert os.listdir("/work/sub") == ["b.txt"]
            os.remove("/work/sub/b.txt")
            assert os.listdir("/work/sub") == []


class TestSynthesizedOpenIsLazy:
    """The synthesized file object is the lazy one, not a whole-file buffer."""

    def test_a_tail_read_costs_one_block(self, termish):
        content = bytes(index % 251 for index in range(1024 * 1024))
        termish.write("/big.bin", content)
        termish.reset_counts()

        with patch(termish):
            with open("/big.bin", "rb") as handle:
                handle.seek(-100, os.SEEK_END)
                tail = handle.read(100)

        assert tail == content[-100:]
        assert len(termish.reads) == 1
        assert termish.bytes_read == BLOCK_SIZE

    def test_opening_reads_nothing(self, termish):
        termish.write("/big.bin", b"x" * 500_000)
        termish.reset_counts()

        with patch(termish):
            handle = open("/big.bin", "rb")
            assert termish.reads == []
            handle.close()


class TestDegradationsWithoutOptionalMethods:
    """A backend with no links reports no links, and says so when asked."""

    def test_islink_and_readlink_are_not_implemented(self, termish):
        termish.write("/a.txt", b"x")
        with patch(termish):
            with pytest.raises(NotImplementedError):
                os.path.islink("/a.txt")
            with pytest.raises(NotImplementedError):
                os.readlink("/a.txt")

    def test_scandir_entries_report_no_links(self, termish):
        termish.write("/a.txt", b"x")
        with patch(termish):
            entries = list(os.scandir("/"))

        assert [entry.name for entry in entries] == ["a.txt"]
        assert entries[0].is_symlink() is False
        assert entries[0].is_file() is True

    def test_getsize_without_the_optional_method(self, termish):
        """``os.path.getsize()`` asks for ``getsize()`` and takes no substitute."""
        termish.write("/a.txt", b"12345")
        with patch(termish):
            with pytest.raises(NotImplementedError):
                os.path.getsize("/a.txt")
            assert os.stat("/a.txt").st_size == 5


class TestWrappersOverABackendWithoutOpen:
    """Composing a bytes-level filesystem must not make it unopenable."""

    def test_mounted(self, termish):
        termish.write("/note.txt", b"mounted")
        fs = MountFS(VirtualFS({}), {"/data": termish})

        assert fs.open("/data/note.txt", "rb").read() == b"mounted"
        with patch(fs):
            assert open("/data/note.txt").read() == "mounted"
            with open("/data/new.txt", "w") as handle:
                handle.write("written")
        assert termish.read("/new.txt") == b"written"

    def test_read_only(self, termish):
        termish.write("/note.txt", b"read me")
        fs = ReadOnlyFS(termish)

        assert fs.open("note.txt", "rb").read() == b"read me"
        with pytest.raises(PermissionError):
            fs.open("note.txt", "w")


class TestABackendWithItsOwnOpen:
    """IsolatedFS has real files behind it and must keep handing them out."""

    def test_open_through_patch_has_a_real_fileno(self, tmp_path):
        fs = IsolatedFS(root=str(tmp_path))

        with patch(fs):
            with open("real.bin", "wb") as handle:
                handle.write(b"payload")

            with open("real.bin", "rb") as handle:
                descriptor = handle.fileno()
                assert isinstance(descriptor, int)
                assert descriptor >= 0
                with mmap.mmap(descriptor, 0, access=mmap.ACCESS_READ) as mapped:
                    assert mapped[:] == b"payload"

    def test_the_handle_is_a_real_file_object(self, tmp_path):
        fs = IsolatedFS(root=str(tmp_path))
        fs.write("real.bin", b"payload")

        with patch(fs):
            with open("real.bin", "rb") as handle:
                assert isinstance(handle, io.BufferedReader)
                assert not isinstance(handle, LazyBinaryFile)

    def test_a_mounted_isolated_filesystem_keeps_its_own_open(self, tmp_path):
        inner = IsolatedFS(root=str(tmp_path))
        inner.write("real.bin", b"payload")
        fs = MountFS(VirtualFS({}), {"/host": inner})

        with fs.open("/host/real.bin", "rb") as handle:
            assert handle.fileno() >= 0
