"""Base filesystem interface and dataclasses.

Defines the common interface for filesystem implementations (VirtualFS, IsolatedFS).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class FileMetadata:
    """Metadata for a single file or directory.

    Attributes:
        size: File size in bytes (0 for directories).
        created_at: ISO 8601 timestamp when file was created (UTC).
        modified_at: ISO 8601 timestamp when file was last modified (UTC).
        is_dir: True if this is a directory, False for files.
    """

    size: int
    created_at: str
    modified_at: str
    is_dir: bool = False

    # os.stat_result-compatible properties — allows FileMetadata to be
    # returned directly from stat() when used with sandtrap's os.stat() patch.

    @property
    def st_size(self) -> int:
        return self.size

    @property
    def st_mode(self) -> int:
        return 0o040755 if self.is_dir else 0o100644

    @property
    def st_ino(self) -> int:
        return 0

    @property
    def st_dev(self) -> int:
        return 0

    @property
    def st_nlink(self) -> int:
        return 1

    @property
    def st_uid(self) -> int:
        import os as _os

        return _os.getuid() if hasattr(_os, "getuid") else 0

    @property
    def st_gid(self) -> int:
        import os as _os

        return _os.getgid() if hasattr(_os, "getgid") else 0

    def _parse_ts(self, iso_str: str) -> float:
        from datetime import datetime

        try:
            return datetime.fromisoformat(iso_str).timestamp()
        except Exception:
            import time

            return time.time()

    @property
    def st_atime(self) -> float:
        return self._parse_ts(self.modified_at)

    @property
    def st_mtime(self) -> float:
        return self._parse_ts(self.modified_at)

    @property
    def st_ctime(self) -> float:
        return self._parse_ts(self.created_at)


@dataclass
class FileInfo:
    """Complete file information for UI display.

    Attributes:
        name: File or directory name (basename).
        path: Full path to file or directory.
        size: File size in bytes (0 for directories).
        created_at: ISO 8601 timestamp when created (UTC).
        modified_at: ISO 8601 timestamp when last modified (UTC).
        is_dir: True if this is a directory, False if file.
    """

    name: str
    path: str
    size: int
    created_at: str
    modified_at: str
    is_dir: bool


@runtime_checkable
class FileSystem(Protocol):
    """Structural interface for filesystem implementations.

    Any object implementing these methods can be used as a filesystem.
    No inheritance required — uses structural (duck) typing.
    """

    def getcwd(self) -> str:
        """Get current working directory."""
        ...

    def chdir(self, path: str) -> None:
        """Change current working directory."""
        ...

    def glob(self, pattern: str) -> list[str]:
        """Return list of paths matching a glob pattern."""
        ...

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """Open a file.

        Args:
            path: File path to open.
            mode: File mode.
            kwargs: Additional arguments.

        Returns:
            File-like object.
        """
        ...

    def read(self, path: str) -> bytes:
        """Read entire file as bytes."""
        ...

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        """Write bytes to file.

        Args:
            path: File path to write.
            content: Bytes to write.
            mode: Write mode ('w' for write/overwrite, 'a' for append).
        """
        ...

    def exists(self, path: str) -> bool:
        """Check if path exists."""
        ...

    def isfile(self, path: str) -> bool:
        """Check if path is a file."""
        ...

    def isdir(self, path: str) -> bool:
        """Check if path is a directory."""
        ...

    def islink(self, path: str) -> bool:
        """Check if path is a symbolic link."""
        ...

    def lexists(self, path: str) -> bool:
        """Check if path exists (without following symlinks)."""
        ...

    def samefile(self, path1: str, path2: str) -> bool:
        """Check if two paths refer to the same file."""
        ...

    def realpath(self, path: str) -> str:
        """Return the canonical path."""
        ...

    def list(self, path: str = ".") -> list[str]:
        """List directory contents (filenames only)."""
        ...

    def list_detailed(self, path: str = ".") -> list[FileInfo]:
        """List directory contents with details."""
        ...

    def listdir(self, path: str = "/", recursive: bool = False) -> list[str]:
        """List directory contents as paths."""
        ...

    def listdir_detailed(
        self, path: str = "/", recursive: bool = False
    ) -> list[FileInfo]:
        """List directory contents with full metadata."""
        ...

    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""
        ...

    def remove(self, path: str) -> None:
        """Remove a file."""
        ...

    def remove_many(self, paths: list[str]) -> None:
        """Remove multiple files."""
        ...

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        ...

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Create directory tree."""
        ...

    def rename(self, src: str, dst: str) -> None:
        """Rename/move a file or directory."""
        ...

    def stat(self, path: str) -> FileMetadata:
        """Get file metadata."""
        ...

    def getsize(self, path: str) -> int:
        """Get file size in bytes."""
        ...

    def write_many(self, files: dict[str, bytes]) -> None:
        """Write multiple files at once."""
        ...

    def get_metadata_snapshot(self) -> dict[str, FileMetadata]:
        """Get snapshot of all file metadata."""
        ...
