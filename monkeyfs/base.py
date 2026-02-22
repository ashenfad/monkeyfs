"""Base filesystem interface and dataclasses.

Defines the common interface for filesystem implementations (VirtualFS, IsolatedFS).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
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
        return os.getuid() if hasattr(os, "getuid") else 0

    @property
    def st_gid(self) -> int:
        return os.getgid() if hasattr(os, "getgid") else 0

    def _parse_ts(self, iso_str: str) -> float:
        try:
            return datetime.fromisoformat(iso_str).timestamp()
        except ValueError:
            return 0.0

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
    """Minimal interface for patch() patching.

    Only methods that the patching layer dispatches to are listed here.
    Implementations may (and typically do) have additional methods like
    read(), write(), glob(), list_detailed(), etc. — those are
    application-level concerns, not part of the interception contract.

    Required — patching will fail without these:
    """

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """Open a file."""
        ...

    def stat(self, path: str) -> FileMetadata:
        """Get file metadata."""
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

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        """List directory contents (filenames only)."""
        ...

    def remove(self, path: str) -> None:
        """Remove a file."""
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

    def getcwd(self) -> str:
        """Get current working directory."""
        ...

    def chdir(self, path: str) -> None:
        """Change current working directory."""
        ...
