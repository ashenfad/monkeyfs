"""Virtual file implementation for buffered writes to state."""

from __future__ import annotations

import io
from collections.abc import MutableMapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .virtual import VirtualFS


class VirtualFile:
    """File-like object that writes to state on close.

    Buffers content during write operations, then persists to state
    when the file is closed (either explicitly or via context manager).

    Attributes:
        path: The virtual filesystem path.
        mode: The file mode ('w', 'wb', 'a', 'ab').
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

        # Use BytesIO for binary, StringIO for text
        if "b" in mode:
            self._buffer: io.BytesIO | io.StringIO = io.BytesIO()
        else:
            self._buffer = io.StringIO()

        # For append mode, load existing content
        if "a" in mode:
            existing = state.get(key)
            if existing is not None:
                if "b" in mode:
                    self._buffer.write(existing)
                else:
                    self._buffer.write(existing.decode("utf-8"))

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
        return self._buffer.write(data)  # type: ignore[arg-type]

    def writelines(self, lines: list[str | bytes]) -> None:
        """Write a list of lines to the buffer."""
        if self._closed:
            raise ValueError(f"I/O operation on closed file: {self._path}")
        self._buffer.writelines(lines)  # type: ignore[arg-type]

    def read(self, size: int = -1) -> str | bytes:
        """Read is not supported for write-only files."""
        raise io.UnsupportedOperation("read")

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

    def flush(self) -> None:
        """Flush is a no-op (content persisted on close)."""
        pass

    def close(self) -> None:
        """Close the file and persist content to state with metadata tracking."""
        if self._closed:
            return

        content = self._buffer.getvalue()
        if isinstance(content, str):
            content = content.encode("utf-8")

        # Use VFS write to get proper metadata tracking
        self._vfs.write(self._path, content)

        self._closed = True

    @property
    def closed(self) -> bool:
        """Return True if the file is closed."""
        return self._closed

    def __enter__(self) -> "VirtualFile":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
