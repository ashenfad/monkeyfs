"""File objects over a filesystem's bytes-level methods.

Two of them, because a read and a write want opposite things from a
filesystem that speaks in whole blobs:

``LazyBinaryFile`` answers a binary read out of ``read(path, offset, size)``
through a small block cache, so a reader that seeks costs the bytes it seeks
to and nothing else.

``VirtualFile`` buffers everything else -- text reads, and every write,
append and update mode -- in memory and writes the whole file back on close,
which is what the protocol's whole-file ``write()`` can express.
"""

from __future__ import annotations

import io
import os
import posixpath
import warnings
from collections import OrderedDict
from collections.abc import Iterable
from typing import Any

#: Bytes fetched per backend call when a read is smaller than this.
#:
#: Sized against the two things that actually read a virtual file. A reader
#: probing a file's shape asks for tens of bytes at a time -- a magic number,
#: a header, a line -- and a block this size turns a scan of them into one
#: backend call instead of hundreds. A parquet footer probe is 64 KiB exactly,
#: so it costs one call and no more bytes than it asked for. Larger would move
#: bytes nobody asked for on every small read; smaller would multiply calls on
#: a backend where a call is an HTTP request.
BLOCK_SIZE = 64 * 1024

#: Blocks kept at once, so the cache cannot grow into a copy of the file.
#: A cache exists here to keep neighbouring small reads from becoming one
#: backend call each, which a handful of blocks does; holding more would be
#: materializing the file again by a slower route.
MAX_CACHED_BLOCKS = 4


class LazyBinaryFile(io.RawIOBase):
    """A seekable binary read stream over a filesystem's ranged ``read()``.

    Opening ``"rb"`` used to hand back an ``io.BytesIO`` over the whole file,
    which made "seekable" a fiction over a buffer someone had already paid
    for: pyarrow reading two parquet columns of twenty seeks to the footer
    and to two column chunks, touching 8% of the file, and every byte of the
    other 92% had already crossed from the backend before it asked. This
    object forwards those seeks instead -- ``read(path, offset, size)`` per
    range -- so the reader's access pattern is what the backend is asked for.

    Reads are served by blocks of ``BLOCK_SIZE``, at most ``MAX_CACHED_BLOCKS``
    of them held at a time, with the block at the end of the file short. A read
    of at least one block's worth skips the cache and issues a single ranged
    read of exactly the range asked for: a reader that wants a megabyte should
    cost one backend call rather than sixteen, and a megabyte held to serve one
    read is not a cache.

    The length of the file comes from ``stat()``, never from reading it, so
    ``seek(-65536, os.SEEK_END)`` costs nothing. ``fileno()`` raises
    ``io.UnsupportedOperation``: there is no file descriptor behind this, and
    readers that ask are prepared for that answer.

    The file is read as it is now, not as it was at open: a block that has
    not been fetched yet comes from the backend when it is reached. Nothing
    in the protocol lets a backend hold a snapshot, so a file rewritten
    underneath an open reader is seen half-and-half, the way it would be
    through a real file descriptor.
    """

    def __init__(self, fs: Any, path: str, block_size: int = BLOCK_SIZE):
        """Open ``path`` on ``fs`` for lazy binary reading.

        Args:
            fs: Filesystem exposing ``read(path, offset, size)`` and ``stat``.
            path: Path to read, as the filesystem understands it.
            block_size: Bytes per cached block.

        Raises:
            FileNotFoundError: If the path has no file, via ``stat()``.
        """
        super().__init__()
        self._fs = fs
        self._path = path
        self._block_size = block_size
        self._size = fs.stat(path).size
        self._pos = 0
        self._blocks: OrderedDict[int, bytes] = OrderedDict()

    # -- stream capabilities --

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def write(self, b: Any) -> int:  # type: ignore[override]
        """Refuse writes; this is a read stream over a backend's bytes."""
        raise io.UnsupportedOperation("write")

    # -- position --

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        """Move the read position, including relative to the end of the file.

        Seeking past the end is allowed, as on a real file; the read that
        follows returns ``b""``.
        """
        self._ensure_open()
        if whence == os.SEEK_SET:
            position = offset
        elif whence == os.SEEK_CUR:
            position = self._pos + offset
        elif whence == os.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError(f"invalid whence: {whence}")
        if position < 0:
            raise ValueError(f"negative seek value {position}")
        self._pos = position
        return position

    def tell(self) -> int:
        self._ensure_open()
        return self._pos

    # -- reading --

    def read(self, size: int | None = -1) -> bytes:  # type: ignore[override]
        """Read up to ``size`` bytes; a negative or absent size reads to EOF."""
        self._ensure_open()
        if size is None or size < 0:
            return self._read_n(self._remaining())
        return self._read_n(size)

    def readall(self) -> bytes:
        """Read from the current position to the end of the file.

        Overridden because ``RawIOBase.readall()`` would otherwise loop over
        8 KiB ``read()`` calls, which is one backend call per 8 KiB for a file
        this object exists to fetch in as few calls as possible.
        """
        self._ensure_open()
        return self._read_n(self._remaining())

    def readinto(self, buffer: Any) -> int:  # type: ignore[override]
        """Fill ``buffer`` with as many bytes as remain, returning the count."""
        self._ensure_open()
        data = self._read_n(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readline(self, size: int | None = -1) -> bytes:  # type: ignore[override]
        """Read one line, scanning blocks rather than byte by byte.

        ``IOBase.readline()`` would find the newline with single-byte reads.
        Each of those is a dictionary lookup here rather than a backend call,
        but a line is still found by scanning the block that holds it, which
        is where a line that straddles a block boundary is stitched together.
        """
        self._ensure_open()
        limit = -1 if size is None or size < 0 else size
        line = bytearray()
        while (limit < 0 or len(line) < limit) and self._remaining() > 0:
            chunk = self._block_at(self._pos)
            if not chunk:
                break
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunk = chunk[: newline + 1]
            if limit >= 0:
                chunk = chunk[: limit - len(line)]
            line += chunk
            self._pos += len(chunk)
            if chunk.endswith(b"\n"):
                break
        return bytes(line)

    def close(self) -> None:
        """Close the stream and drop the cached blocks."""
        if not self.closed:
            self._blocks.clear()
        super().close()

    # -- internals --

    def _ensure_open(self) -> None:
        if self.closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")

    def _remaining(self) -> int:
        return max(self._size - self._pos, 0)

    def _read_n(self, count: int) -> bytes:
        """Read ``count`` bytes from the current position, truncating at EOF."""
        count = min(count, self._remaining())
        if count <= 0:
            return b""
        if count >= self._block_size:
            # One ranged read for exactly what was asked for, uncached.
            data = self._fs.read(self._path, self._pos, count)
            self._pos += len(data)
            return data

        out = bytearray()
        while count > 0:
            chunk = self._block_at(self._pos)[:count]
            if not chunk:
                break  # the file is shorter than stat() said; stop at what is
            out += chunk
            self._pos += len(chunk)
            count -= len(chunk)
        return bytes(out)

    def _block_at(self, position: int) -> bytes:
        """The cached block holding ``position``, from that position onward."""
        index = position // self._block_size
        block = self._blocks.get(index)
        if block is None:
            start = index * self._block_size
            block = self._fs.read(self._path, start, self._block_size)
            self._blocks[index] = block
            while len(self._blocks) > MAX_CACHED_BLOCKS:
                self._blocks.popitem(last=False)
        else:
            self._blocks.move_to_end(index)
        return block[position - index * self._block_size :]


class VirtualFile:
    """File-like object that writes to state on close.

    Buffers content during write operations, then persists mutations to state
    when the file is closed (either explicitly or via context manager).

    This one materializes and stays that way, for two reasons that do not
    apply to a binary read. A write is whole-file by the protocol's own
    design -- ``write(path, content)`` replaces a file and there is no ranged
    write to buffer toward -- so a write mode has to hold the content it will
    send anyway. And a text read would have to decode across block
    boundaries, where a multi-byte character can be split in half: the
    bookkeeping to stitch one back together buys nothing a caller reading
    text was going to skip past.

    Attributes:
        path: The virtual filesystem path.
        mode: The file mode ('w', 'wb', 'a', 'ab', 'r+', 'rb+').
    """

    def __init__(self, fs: Any, path: str, mode: str):
        """Initialize a buffered virtual file over any filesystem.

        Args:
            fs: Filesystem exposing ``read(path)`` and ``write(path, content)``.
                The whole file is read back from it on close, so it is the
                filesystem that gets the metadata tracking right, not this.
            path: File path, as the filesystem understands it.
            mode: File open mode.
        """
        self._fs = fs
        self._path = path
        self._mode = mode
        self._closed = False

        # A truncating mode replaces the file, so its current content is not
        # worth fetching: on a backend where a read crosses a wire, asking
        # for bytes about to be discarded is the whole cost of the write.
        existing = _read_or_none(fs, path) if ("a" in mode or "r" in mode) else None
        # Opening w/x mutates the file even without a subsequent write. Opening
        # a missing file in append mode creates it; an existing append file can
        # remain clean until data is written.
        self._dirty = "w" in mode or "x" in mode or ("a" in mode and existing is None)

        # Use BytesIO for binary, StringIO for text
        if "b" in mode:
            self._buffer: io.BytesIO | io.StringIO = io.BytesIO()
        else:
            self._buffer = io.StringIO()

        # Append and read/update modes start with the existing content loaded.
        if "a" in mode or "r" in mode:
            if existing is not None:
                if "b" in mode:
                    self._buffer.write(existing)
                else:
                    self._buffer.write(existing.decode("utf-8"))

        # Read/update starts at the beginning; append starts at the end.
        if "r" in mode:
            self._buffer.seek(0)

    def write(self, data: str | bytes) -> int:
        """Write data to the buffer.

        Args:
            data: Content to write (str for text mode, bytes for binary).

        Returns:
            Number of characters/bytes written.

        Raises:
            ValueError: If file is already closed.
        """
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        written = self._buffer.write(data)  # type: ignore[arg-type]
        if written:
            self._dirty = True
        return written

    def writelines(self, lines: Iterable[str | bytes]) -> None:
        """Write lines from an iterable to the buffer."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        self._buffer.writelines(lines)  # type: ignore[arg-type]
        self._dirty = True

    def read(self, size: int = -1) -> str | bytes:
        """Read data from an update-mode file."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        if "+" not in self._mode:
            raise io.UnsupportedOperation("read")
        return self._buffer.read(size)

    def readline(self, size: int = -1) -> str | bytes:
        """Read one line from an update-mode file."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        if "+" not in self._mode:
            raise io.UnsupportedOperation("read")
        return self._buffer.readline(size)

    def readlines(self, hint: int = -1) -> list[str] | list[bytes]:
        """Read lines from an update-mode file."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        if "+" not in self._mode:
            raise io.UnsupportedOperation("read")
        return self._buffer.readlines(hint)

    def __iter__(self) -> "VirtualFile":
        """Return this update-mode file as a line iterator."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        if "+" not in self._mode:
            raise io.UnsupportedOperation("read")
        return self

    def __next__(self) -> str | bytes:
        """Read the next line from an update-mode file."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        if "+" not in self._mode:
            raise io.UnsupportedOperation("read")
        return next(self._buffer)

    def seek(self, offset: int, whence: int = 0) -> int:
        """Seek to a position in the buffer."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        return self._buffer.seek(offset, whence)

    def tell(self) -> int:
        """Return current position in the buffer."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        return self._buffer.tell()

    def truncate(self, size: int | None = None) -> int:
        """Resize the buffered file and persist the change on close."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        previous_size = len(self._buffer.getvalue())
        new_size = self._buffer.truncate(size)
        if new_size != previous_size:
            self._dirty = True
        return new_size

    def flush(self) -> None:
        """Flush is a no-op (content persisted on close)."""
        pass

    def close(self) -> None:
        """Close the file and persist content through the filesystem's write()."""
        if self._closed:
            return

        if self._dirty:
            content = self._buffer.getvalue()
            if isinstance(content, str):
                content = content.encode("utf-8")

            # Through the filesystem's own write(), so that whatever metadata
            # it tracks is updated the way a direct write would update it.
            self._fs.write(self._path, content)

        self._closed = True

    def __del__(
        self,
        _warn=warnings.warn,
        _ResourceWarning=ResourceWarning,
    ) -> None:
        """Best-effort flush on garbage collection.

        Real file objects persist buffered writes when finalized without an
        explicit close(); VirtualFile matches that so `open(p, "w").write(x)`
        does not silently lose data. A ResourceWarning is emitted, mirroring
        CPython's unclosed-file warning. Explicit close() (or a context
        manager) remains the reliable path -- finalization order during
        interpreter shutdown is not guaranteed.

        ``_warn``/``_ResourceWarning`` are bound as defaults because module
        globals may already be cleared when finalizers run at interpreter
        shutdown (the ``subprocess.Popen.__del__`` idiom).
        """
        if getattr(self, "_closed", True):
            return
        try:
            self.close()
            _warn(
                f"unclosed file {self._path!r}; buffered content was "
                "persisted at garbage collection",
                _ResourceWarning,
                stacklevel=2,
                source=self,
            )
        except Exception:
            pass  # never raise from __del__ (e.g. interpreter teardown)

    @property
    def closed(self) -> bool:
        """Return True if the file is closed."""
        return self._closed

    def __enter__(self) -> "VirtualFile":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _read_or_none(fs: Any, path: str) -> bytes | None:
    """The file's current bytes, or ``None`` where there is no file."""
    try:
        return fs.read(path)
    except FileNotFoundError:
        return None


def _parent_dir(fs: Any, path: str) -> str:
    """The absolute parent directory of ``path``, or ``""`` at the root.

    A relative path is resolved against the filesystem's own working
    directory before its parent is taken. ``resolve_path`` is preferred
    where the backend has one; without it, ``getcwd`` is joined on, since
    it is required of every backend and a relative path means "from here"
    -- taking the parent of the bare path would check ``/sub`` for a
    caller in ``/work`` asking about ``sub/file``.
    """
    resolve = getattr(fs, "resolve_path", None)
    if resolve is not None:
        resolved = resolve(path)
    elif path.startswith("/"):
        resolved = path
    else:
        resolved = posixpath.join(fs.getcwd(), path)
    parent = posixpath.dirname(posixpath.normpath(resolved).lstrip("/"))
    return "/" + parent if parent else ""


def open_file(fs: Any, path: str, mode: str = "r", **kwargs: Any) -> Any:
    """Open ``path`` on a filesystem using only its bytes-level methods.

    This is the ``open()`` monkeyfs provides over a backend that has none of
    its own: everything here is built from ``read``, ``write``, ``stat``,
    ``exists`` and ``isfile``, so a filesystem that answers those gets a
    working ``builtins.open()`` -- lazy in ``"rb"``, buffered everywhere
    else -- without implementing a file object.

    A backend that *has* an ``open()`` is asked for it instead, by the patch
    layer and by both wrappers. A real directory can hand back a real file
    descriptor, and streaming writes, ``mmap`` and files larger than memory
    all work there; routing those through a buffer would narrow the one
    backend that has real files.

    Args:
        fs: The filesystem to open on.
        path: File path, as that filesystem understands it.
        mode: File mode ('r', 'rb', 'w', 'wb', 'a', 'ab', 'x', 'r+', 'rb+').
        **kwargs: Accepted and ignored, the way the in-tree backends accept
            and ignore ``encoding`` and friends; text is UTF-8.

    Returns:
        A file-like object: ``LazyBinaryFile`` for a binary read, an
        ``io.StringIO`` for a text read, a ``VirtualFile`` otherwise.

    Raises:
        FileNotFoundError: Reading a file that is not there, or creating one
            whose parent directory is not there (POSIX open()).
        FileExistsError: Exclusive creation over an existing path.
        ValueError: If the mode is not a mode.
    """
    if "r" in mode and not any(c in mode for c in "+wax"):
        if "b" in mode:
            # Nothing is read here; the reader's own seeks decide that.
            if not fs.isfile(path):
                raise FileNotFoundError(path)
            return LazyBinaryFile(fs, path)
        content = _read_or_none(fs, path)
        if content is None:
            raise FileNotFoundError(path)
        return io.StringIO(content.decode("utf-8"))

    if "w" in mode or "a" in mode or "x" in mode or ("r" in mode and "+" in mode):
        if "r" in mode and "+" in mode and not fs.isfile(path):
            raise FileNotFoundError(path)

        if "x" in mode and fs.exists(path):
            raise FileExistsError(f"[Errno 17] File exists: '{path}'")

        # POSIX open() fails with ENOENT on a missing parent rather than
        # creating the tree, and a direct write() that creates parents for
        # convenience must not make open() more forgiving than the real one.
        parent = _parent_dir(fs, path)
        if parent and not fs.isdir(parent):
            raise FileNotFoundError(f"No such file or directory: '{path}'")

        return VirtualFile(fs, path, mode)

    raise ValueError(f"Invalid mode: {mode}")
