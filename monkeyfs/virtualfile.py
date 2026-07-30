"""Virtual file implementation for buffered writes to state."""

from __future__ import annotations

import io
import warnings
from collections.abc import Iterable, MutableMapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .virtual import VirtualFS


class VirtualFile:
    """File-like object that writes to state on close.

    Buffers content during write operations, then persists mutations to state
    when the file is closed (either explicitly or via context manager).

    Attributes:
        path: The virtual filesystem path.
        mode: The file mode ('w', 'wb', 'a', 'ab', 'r+', 'rb+').
    """

    def __init__(
        self,
        vfs: "VirtualFS",
        state: MutableMapping[str, bytes],
        key: str,
        path: str,
        mode: str,
    ):
        """Initialize a writable virtual file.

        Args:
            vfs: The VirtualFS instance for metadata tracking.
            state: State backend for persistence.
            key: Encoded state key for this file.
            path: Original file path (for error messages).
            mode: File open mode.
        """
        self._vfs = vfs
        self._state = state
        self._key = key
        self._path = path
        self._mode = mode
        self._closed = False

        existing = state.get(key)
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
        """Close the file and persist content to state with metadata tracking."""
        if self._closed:
            return

        if self._dirty:
            content = self._buffer.getvalue()
            if isinstance(content, str):
                content = content.encode("utf-8")

            # Use VFS write to get proper metadata tracking.
            self._vfs.write(self._path, content)

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
