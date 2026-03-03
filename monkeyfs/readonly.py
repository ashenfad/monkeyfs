"""Read-only filesystem wrapper.

Wraps any FileSystem and blocks all write operations with PermissionError.
Read operations delegate transparently via __getattr__.
"""

from __future__ import annotations

import os
from typing import Any


class ReadOnlyFS:
    """Wraps any FileSystem and blocks all write operations.

    Read operations delegate transparently to the wrapped filesystem.
    Write operations raise PermissionError.

    Example:
        >>> from monkeyfs import VirtualFS, ReadOnlyFS
        >>> vfs = VirtualFS({})
        >>> vfs.write("data.csv", b"a,b,c")
        >>> ro = ReadOnlyFS(vfs)
        >>> ro.read("data.csv")
        b'a,b,c'
        >>> ro.write("x.txt", b"hi")  # raises PermissionError
    """

    def __init__(self, fs: Any):
        self._fs = fs

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fs, name)

    @staticmethod
    def _deny() -> None:
        raise PermissionError("Read-only filesystem")

    # -- Mode-sensitive operations --

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        if any(c in mode for c in "wax+"):
            self._deny()
        return self._fs.open(path, mode, **kwargs)

    def access(self, path: str, mode: int) -> bool:
        if mode & os.W_OK:
            return False
        return self._fs.access(path, mode)

    # -- Blocked mutating operations --

    def remove(self, path: str) -> None:
        self._deny()

    def mkdir(self, path: str, **kwargs: Any) -> None:
        self._deny()

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        self._deny()

    def rename(self, src: str, dst: str) -> None:
        self._deny()

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        self._deny()

    def write_many(self, files: dict[str, bytes]) -> None:
        self._deny()

    def remove_many(self, paths: list[str]) -> None:
        self._deny()

    def rmdir(self, path: str) -> None:
        self._deny()

    def replace(self, src: str, dst: str) -> None:
        self._deny()

    def symlink(self, src: str, dst: str) -> None:
        self._deny()

    def link(self, src: str, dst: str) -> None:
        self._deny()

    def chmod(self, path: str, mode: int) -> None:
        self._deny()

    def chown(self, path: str, uid: int, gid: int) -> None:
        self._deny()

    def truncate(self, path: str, length: int) -> None:
        self._deny()
