"""A conformance kit for filesystem backends.

``check_filesystem(fs)`` runs a backend through the protocol the patch layer
expects of it and raises ``AssertionError`` on the first thing that is wrong,
naming the method and what was expected of it. It is shipped inside the
package, stdlib-only, so that writing a backend needs this library and
nothing else -- not a test harness, not the library on the other side of the
protocol.

What it checks, in order: the required methods exist; directories,
``getcwd``/``chdir`` and relative resolution behave, ``exist_ok`` included;
a file written comes back through ``stat()``, ``list()`` and ``read()``;
``list_detailed()`` names each entry in the namespace it was asked in; the
ranged read means what it says, including the cases a backend that quietly
ignores ``offset`` and ``size`` would fail; ``open()`` round-trips bytes and
text, through the backend's own if it has one and through the synthesized one
under ``patch()`` if it does not; and ``rename``/``remove`` leave the
filesystem as they found it.

Usage::

    from monkeyfs import check_filesystem

    check_filesystem(MyFileSystem())   # an empty one; it writes and cleans up
"""

from __future__ import annotations

from typing import Any

from .base import REQUIRED_METHODS

__all__ = ["check_filesystem"]

#: Where the kit works. Named rather than random so that a backend left
#: dirty by a failing check can be found and cleared by hand.
SCRATCH = "/monkeyfs_conformance"

#: Content with every byte value in it, long enough to have a middle and an
#: end that are distinguishable from the start -- which is exactly what a
#: backend ignoring ``offset`` and ``size`` gets wrong.
CONTENT = bytes(range(256)) * 8


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _has(fs: Any, name: str) -> bool:
    return callable(getattr(fs, name, None))


def check_filesystem(fs: Any, scratch: str = SCRATCH) -> None:
    """Check that ``fs`` implements the monkeyfs backend protocol.

    Args:
        fs: An empty filesystem. The kit writes under ``scratch`` and removes
            what it wrote, but it is not a test double for a filesystem
            holding real content -- run it against a fresh one.
        scratch: Directory to work in. It must not already exist.

    Raises:
        AssertionError: On the first violation, naming the method and what
            was expected of it.
    """
    _check_required_methods(fs)
    _require(
        not fs.exists(scratch),
        f"check_filesystem() needs an empty filesystem: {scratch!r} already "
        f"exists. Pass a fresh backend, or a scratch path that is free.",
    )

    try:
        _check_directories(fs, scratch)
        _check_working_directory(fs, scratch)
        path = f"{scratch}/probe.bin"
        _write(fs, path, CONTENT)
        _check_file_metadata(fs, scratch, path)
        _check_listing_paths(fs, scratch)
        _check_ranged_read(fs, path)
        _check_open(fs, scratch, path)
        _check_rename_and_remove(fs, scratch, path)
    finally:
        _cleanup(fs, scratch)


def _check_required_methods(fs: Any) -> None:
    missing = sorted(name for name in REQUIRED_METHODS if not _has(fs, name))
    _require(
        not missing,
        f"{type(fs).__name__} is missing required method(s) {missing}. The "
        f"patch layer calls these unconditionally, so a backend without one "
        f"cannot be patched.",
    )
    _require(
        _has(fs, "read") and _has(fs, "write"),
        f"{type(fs).__name__} must implement read() and write(): they are "
        f"what every file object over this filesystem is built from, and "
        f"what a shell over it reads and writes with.",
    )


def _check_directories(fs: Any, scratch: str) -> None:
    fs.makedirs(scratch)
    _require(
        fs.isdir(scratch) and fs.exists(scratch),
        f"after makedirs({scratch!r}), isdir() and exists() must both be True",
    )
    _require(
        not fs.isfile(scratch),
        f"isfile({scratch!r}) must be False for a directory",
    )
    _require(
        fs.list(scratch) == [],
        f"list({scratch!r}) must be empty for a directory with nothing in it, "
        f"got {fs.list(scratch)!r}",
    )

    nested = f"{scratch}/nested"
    fs.mkdir(nested)
    _require(
        fs.isdir(nested),
        f"after mkdir({nested!r}), isdir() must be True",
    )
    _require(
        fs.list(scratch) == ["nested"],
        f"list({scratch!r}) must name its children and nothing else, got "
        f"{fs.list(scratch)!r} -- names, not paths",
    )

    _check_exist_ok(fs, scratch, nested)

    missing = f"{scratch}/not-there"
    _require(
        not fs.exists(missing) and not fs.isfile(missing) and not fs.isdir(missing),
        f"exists(), isfile() and isdir() must all be False for {missing!r}",
    )


def _check_exist_ok(fs: Any, scratch: str, nested: str) -> None:
    """A directory that is already there is an error unless asked to be fine.

    ``exist_ok`` is the only thing that makes creating an existing directory
    succeed, and a caller reaches for False precisely to be told. A backend
    that returns silently instead hands that caller the answer it asked to
    be spared.
    """
    fs.makedirs(scratch)
    _require(
        fs.isdir(scratch),
        f"makedirs({scratch!r}) on a directory that already exists must be "
        f"silent -- exist_ok defaults to True -- and must leave it a directory",
    )

    cases = [
        (
            lambda: fs.makedirs(scratch, exist_ok=False),
            f"makedirs({scratch!r}, exist_ok=False) on an existing directory "
            f"must raise FileExistsError",
        ),
        (
            lambda: fs.mkdir(nested),
            f"mkdir({nested!r}) on an existing directory must raise "
            f"FileExistsError -- exist_ok defaults to False",
        ),
        (
            lambda: fs.mkdir(nested, parents=True, exist_ok=False),
            f"mkdir({nested!r}, parents=True, exist_ok=False) on an existing "
            f"directory must raise FileExistsError: parents says how to build "
            f"the tree, not whether the end of it may already be there",
        ),
    ]
    for call, message in cases:
        try:
            call()
        except FileExistsError:
            continue
        except Exception as error:  # noqa: BLE001 - reported as the failure
            raise AssertionError(
                f"{message}, not {type(error).__name__} ({error})"
            ) from error
        raise AssertionError(f"{message}, rather than returning silently")

    fs.mkdir(nested, exist_ok=True)
    _require(
        fs.isdir(nested),
        f"mkdir({nested!r}, exist_ok=True) on an existing directory must be "
        f"silent and must leave it a directory",
    )


def _check_listing_paths(fs: Any, scratch: str) -> None:
    """``FileInfo.path``: the directory as queried, joined with the entry.

    Listing ``"/src"`` names ``"/src/lib/util.py"`` and listing ``"src"``
    names ``"src/lib/util.py"``, so a path that comes out of a listing goes
    back in, and an absolute query never answers in anything but this
    filesystem's own absolute paths.
    """
    if not _has(fs, "list_detailed"):
        return

    leaf = f"{scratch}/nested/leaf.bin"
    _write(fs, leaf, b"leaf")
    try:
        absolute = {info.name: info.path for info in fs.list_detailed(scratch)}
        _require(
            absolute.get("probe.bin") == f"{scratch}/probe.bin",
            f"list_detailed({scratch!r})[...].path must be the queried "
            f"directory joined with the entry, {scratch + '/probe.bin'!r}, got "
            f"{absolute.get('probe.bin')!r}",
        )

        deep = [info.path for info in fs.list_detailed(scratch, True)]
        _require(
            f"{scratch}/nested/leaf.bin" in deep,
            f"a recursive list_detailed({scratch!r}) must name a nested entry "
            f"{scratch + '/nested/leaf.bin'!r}, got {deep!r}",
        )

        start = fs.getcwd()
        fs.chdir(scratch)
        try:
            relative = [info.path for info in fs.list_detailed("nested")]
            _require(
                relative == ["nested/leaf.bin"],
                f"list_detailed('nested') must answer in the namespace it was "
                f"asked in: a relative query gives paths relative to the same "
                f"place, ['nested/leaf.bin'], got {relative!r}",
            )
        finally:
            fs.chdir(start)
    finally:
        fs.remove(leaf)


def _check_working_directory(fs: Any, scratch: str) -> None:
    start = fs.getcwd()
    _require(
        isinstance(start, str),
        f"getcwd() must return a str, got {type(start).__name__}",
    )

    fs.chdir(scratch)
    moved = fs.getcwd()
    _require(
        fs.isdir(moved),
        f"after chdir({scratch!r}), getcwd() returned {moved!r}, which isdir() "
        f"says is not a directory",
    )

    _write(fs, "relative.bin", b"relative")
    _require(
        fs.isfile(f"{scratch}/relative.bin"),
        f"a relative path must resolve against getcwd(): writing "
        f"'relative.bin' from {moved!r} did not land in {scratch!r}",
    )
    fs.remove("relative.bin")

    fs.chdir(start)
    _require(
        fs.getcwd() == start,
        f"chdir({start!r}) must restore the working directory, got {fs.getcwd()!r}",
    )


def _check_file_metadata(fs: Any, scratch: str, path: str) -> None:
    _require(
        fs.isfile(path) and fs.exists(path) and not fs.isdir(path),
        f"after writing {path!r}, isfile() and exists() must be True and isdir() False",
    )
    meta = fs.stat(path)
    _require(
        meta.size == len(CONTENT),
        f"stat({path!r}).size must be {len(CONTENT)}, got {meta.size}. A file "
        f"object reads to the end of the file by this number, so a wrong one "
        f"truncates every read.",
    )
    _require(
        meta.is_dir is False,
        f"stat({path!r}).is_dir must be False for a file",
    )
    _require(
        "probe.bin" in fs.list(scratch),
        f"list({scratch!r}) must include 'probe.bin', got {fs.list(scratch)!r}",
    )

    try:
        fs.stat(f"{scratch}/not-there")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("stat() must raise FileNotFoundError for a missing path")


def _check_ranged_read(fs: Any, path: str) -> None:
    """The ranged read, including what an unranged backend gets wrong."""
    size = len(CONTENT)
    cases = [
        ((path,), CONTENT, "read(path) must return the whole file"),
        (
            (path, 0, -1),
            CONTENT,
            "read(path, 0, -1) must return the whole file: a negative size "
            "means to the end",
        ),
        (
            (path, 100),
            CONTENT[100:],
            "read(path, 100) must skip the first 100 bytes and read to the "
            "end; a backend that ignores offset returns the whole file",
        ),
        (
            (path, 100, 50),
            CONTENT[100:150],
            "read(path, 100, 50) must return exactly the 50 bytes at offset "
            "100; a backend that ignores offset and size returns the whole file",
        ),
        (
            (path, 0, 10),
            CONTENT[:10],
            "read(path, 0, 10) must return the first 10 bytes only",
        ),
        (
            (path, 7, 0),
            b"",
            "read(path, 7, 0) must return b'': a size of zero asks for nothing",
        ),
        (
            (path, size - 10, 1000),
            CONTENT[-10:],
            "a range running past the end must be truncated there, not refused",
        ),
        (
            (path, size),
            b"",
            "a read starting exactly at the end of the file must return b''",
        ),
        (
            (path, size + 100),
            b"",
            "a read starting past the end of the file must return b''",
        ),
        (
            (path, size + 100, 10),
            b"",
            "a sized read starting past the end of the file must return b''",
        ),
    ]
    for args, expected, message in cases:
        try:
            got = fs.read(*args)
        except TypeError as error:
            raise AssertionError(
                f"read() must accept offset and size: read(*{args!r}) raised "
                f"TypeError ({error}). The signature is "
                f"read(path, offset=0, size=-1)."
            ) from error
        _require(
            got == expected,
            f"{message}. read(*{args!r}) returned {len(got)} bytes, expected "
            f"{len(expected)}.",
        )

    _require(
        fs.read(path, offset=100, size=50) == CONTENT[100:150],
        "offset and size must be usable as keyword arguments",
    )

    try:
        fs.read(path, -1)
    except ValueError:
        pass
    else:
        raise AssertionError(
            "read(path, -1) must raise ValueError: a file has no bytes before "
            "its start, and clamping to zero hands back data nobody asked for"
        )

    try:
        fs.read(f"{path}.missing", 0, 10)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError(
            "read() on a missing path must raise FileNotFoundError, range or no range"
        )


def _check_open(fs: Any, scratch: str, path: str) -> None:
    """``open()``: the backend's own where it has one, synthesized otherwise."""
    if _has(fs, "open"):
        with fs.open(path, "rb") as handle:
            _require(
                handle.read() == CONTENT,
                f"open({path!r}, 'rb').read() must return the file's bytes",
            )
        with fs.open(path, "rb") as handle:
            handle.seek(-10, 2)
            _require(
                handle.read() == CONTENT[-10:],
                f"open({path!r}, 'rb') must return a seekable stream: "
                f"seek(-10, SEEK_END) then read() must give the last 10 bytes",
            )

        text_path = f"{scratch}/probe.txt"
        with fs.open(text_path, "w") as handle:
            handle.write("hello\nthere\n")
        with fs.open(text_path, "r") as handle:
            _require(
                handle.read() == "hello\nthere\n",
                f"open({text_path!r}, 'w') then 'r' must round-trip text",
            )
        fs.remove(text_path)
        return

    # No open() of its own: the patch layer synthesizes one, and that is the
    # open() this filesystem's users will actually get.
    from .patching import patch

    with patch(fs):
        with open(path, "rb") as handle:
            _require(
                handle.read() == CONTENT,
                "under patch(), open(path, 'rb').read() must return the file's bytes",
            )
            handle.seek(-10, 2)
            _require(
                handle.read() == CONTENT[-10:],
                "under patch(), a binary read must be seekable from the end",
            )

        text_path = f"{scratch}/probe.txt"
        with open(text_path, "w") as handle:
            handle.write("hello\nthere\n")
        with open(text_path, "r") as handle:
            _require(
                handle.read() == "hello\nthere\n",
                "under patch(), open(path, 'w') then 'r' must round-trip text",
            )

        binary_path = f"{scratch}/probe2.bin"
        with open(binary_path, "wb") as handle:
            handle.write(CONTENT)

    _require(
        fs.read(binary_path) == CONTENT,
        f"bytes written through the synthesized open() must reach the "
        f"filesystem: read({binary_path!r}) did not return them",
    )
    fs.remove(text_path)
    fs.remove(binary_path)


def _check_rename_and_remove(fs: Any, scratch: str, path: str) -> None:
    moved = f"{scratch}/moved.bin"
    fs.rename(path, moved)
    _require(
        fs.isfile(moved) and not fs.isfile(path),
        f"after rename({path!r}, {moved!r}) the file must be at the new path "
        f"and gone from the old one",
    )
    _require(
        fs.read(moved) == CONTENT,
        "rename() must move the content, not just the name",
    )

    fs.remove(moved)
    _require(
        not fs.exists(moved),
        f"after remove({moved!r}), exists() must be False",
    )

    try:
        fs.remove(moved)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError(
            "remove() on a missing path must raise FileNotFoundError rather "
            "than succeeding silently"
        )


def _write(fs: Any, path: str, content: bytes) -> None:
    fs.write(path, content)


def _cleanup(fs: Any, scratch: str) -> None:
    """Leave the filesystem as it was found, as far as the protocol allows."""
    try:
        for name in fs.list(scratch, True) if fs.exists(scratch) else []:
            target = f"{scratch}/{name}"
            if fs.isfile(target):
                fs.remove(target)
        if _has(fs, "rmdir"):
            for name in sorted(fs.list(scratch, True), key=len, reverse=True):
                fs.rmdir(f"{scratch}/{name}")
            fs.rmdir(scratch)
    except Exception:
        # Cleanup runs in a finally, where raising would replace the
        # AssertionError that says what is actually wrong with the backend.
        pass
