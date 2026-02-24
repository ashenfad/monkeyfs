"""Virtual file descriptor table for low-level fd emulation.

Maps fake fd integers (starting at 10,000) to in-memory BytesIO buffers
backed by VFS state. Enables os.open/os.read/os.write/os.close to work
under the virtual filesystem.
"""

from __future__ import annotations

import errno
import io
import os
import threading
from dataclasses import dataclass
from typing import Any


@dataclass
class VirtualFD:
    """State for a single virtual file descriptor."""

    path: str
    fs: Any
    buffer: io.BytesIO
    flags: int
    mode: int
    writable: bool
    readable: bool
    closed: bool = False


class VirtualFDTable:
    """Thread-safe virtual file descriptor table.

    Allocates fake fd numbers starting from 10,000 to avoid collisions
    with real OS file descriptors.
    """

    _BASE_FD = 10_000

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._table: dict[int, VirtualFD] = {}
        self._next_fd = self._BASE_FD

    def allocate(self, path: str, fs: Any, flags: int, file_mode: int) -> int:
        """Create a new virtual fd for the given path.

        Handles O_CREAT, O_EXCL, O_TRUNC, O_APPEND, O_RDONLY/O_WRONLY/O_RDWR.
        Returns the allocated fd number.
        """
        access = flags & 0o3  # O_RDONLY=0, O_WRONLY=1, O_RDWR=2
        readable = access in (os.O_RDONLY, os.O_RDWR)
        writable = access in (os.O_WRONLY, os.O_RDWR)

        # Resolve path
        if hasattr(fs, "resolve_path"):
            resolved = fs.resolve_path(path)
        else:
            resolved = path

        file_exists = fs.exists(resolved)

        # O_CREAT | O_EXCL: fail if file exists
        if (flags & os.O_CREAT) and (flags & os.O_EXCL):
            if file_exists:
                raise FileExistsError(errno.EEXIST, "File exists", path)

        # Non-O_CREAT open on nonexistent file
        if not (flags & os.O_CREAT) and not file_exists:
            raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)

        # Load existing content into buffer
        buf = io.BytesIO()
        if file_exists and not (flags & os.O_TRUNC):
            try:
                content = fs.read(resolved)
                buf.write(content)
                if not (flags & os.O_APPEND):
                    buf.seek(0)
                # O_APPEND: position stays at end
            except FileNotFoundError:
                pass

        # O_CREAT on new file: register it in VFS
        if (flags & os.O_CREAT) and not file_exists:
            parent = os.path.dirname(resolved)
            if parent and parent != "/" and hasattr(fs, "makedirs"):
                fs.makedirs(parent, exist_ok=True)
            from .core import _in_vfs_operation

            token = _in_vfs_operation.set(True)
            try:
                fs.write(resolved, b"")
            finally:
                _in_vfs_operation.reset(token)

        with self._lock:
            fd = self._next_fd
            self._next_fd += 1
            self._table[fd] = VirtualFD(
                path=resolved,
                fs=fs,
                buffer=buf,
                flags=flags,
                mode=file_mode,
                writable=writable,
                readable=readable,
            )

        return fd

    def get(self, fd: int) -> VirtualFD | None:
        """Look up a virtual fd. Returns None if not in the table."""
        return self._table.get(fd)

    def is_virtual(self, fd: int) -> bool:
        """Check if fd is in the virtual table."""
        return fd in self._table

    def close(self, fd: int) -> None:
        """Close and deallocate a virtual fd.

        Flushes content to VFS if the fd was writable.
        """
        with self._lock:
            vfd = self._table.pop(fd, None)

        if vfd is None:
            raise OSError(errno.EBADF, "Bad file descriptor")

        if vfd.closed:
            return

        vfd.closed = True

        if vfd.writable:
            content = vfd.buffer.getvalue()
            from .core import _in_vfs_operation

            token = _in_vfs_operation.set(True)
            try:
                vfd.fs.write(vfd.path, content)
            finally:
                _in_vfs_operation.reset(token)


class VirtualFDRawIO(io.RawIOBase):
    """RawIOBase backed by a virtual fd's buffer.

    Used by _wrap_virtual_fd to produce buffered/text file objects
    that Python's io layer can work with (BufferedWriter, TextIOWrapper, etc.).
    """

    def __init__(self, fd: int, table: VirtualFDTable) -> None:
        self._fd = fd
        self._table = table
        vfd = table.get(fd)
        if vfd is None:
            raise OSError(errno.EBADF, "Bad file descriptor")
        self._vfd = vfd
        self.name = vfd.path

    def readinto(self, b: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self._vfd.buffer.read(len(b))
        n = len(data)
        b[:n] = data
        return n

    def write(self, b: bytes | bytearray | memoryview) -> int:  # type: ignore[override]
        return self._vfd.buffer.write(bytes(b))

    def readable(self) -> bool:
        return self._vfd.readable

    def writable(self) -> bool:
        return self._vfd.writable

    def seekable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._vfd.buffer.seek(offset, whence)

    def tell(self) -> int:
        return self._vfd.buffer.tell()

    def fileno(self) -> int:
        return self._fd

    def close(self) -> None:
        if not self.closed:
            super().close()
            if self._table.is_virtual(self._fd):
                self._table.close(self._fd)


def _wrap_virtual_fd(fd: int, mode: str, table: VirtualFDTable, **kwargs: Any) -> Any:
    """Wrap a virtual fd in a buffered/text file object matching the mode."""
    raw = VirtualFDRawIO(fd, table)

    if "b" in mode:
        if "+" in mode:
            return io.BufferedRandom(raw)
        elif "r" in mode:
            return io.BufferedReader(raw)
        else:
            return io.BufferedWriter(raw)
    else:
        # Text mode — wrap in appropriate buffered layer + TextIOWrapper
        encoding = kwargs.get("encoding") or "utf-8"
        errors = kwargs.get("errors")
        newline = kwargs.get("newline")
        if "+" in mode:
            buffered: io.BufferedIOBase = io.BufferedRandom(raw)
        elif "w" in mode or "a" in mode or "x" in mode:
            buffered = io.BufferedWriter(raw)
        else:
            buffered = io.BufferedReader(raw)
        return io.TextIOWrapper(
            buffered, encoding=encoding, errors=errors, newline=newline
        )


# Module-level singleton
_fd_table = VirtualFDTable()
