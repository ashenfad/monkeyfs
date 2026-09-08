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


# -----------------------------------------------------------------------------
# The method surface, as sets.
#
# These are the single source of truth for who may call what. The patch layer
# dispatches to the required and optional names below; ReadOnlyFS derives its
# allowlist from them and MountFS derives what it forwards, so a method added
# here reaches every wrapper instead of being remembered in three places. A
# name in no set is refused by ReadOnlyFS and unreachable through the patch
# layer, which is the failure a wrapper should have.
# -----------------------------------------------------------------------------

#: Dispatched unconditionally by the patch layer; a backend without one of
#: these cannot be patched. ``chdir`` is here as a read: it moves the
#: filesystem's own working directory and stores nothing.
REQUIRED_READ_METHODS = frozenset(
    {"chdir", "exists", "getcwd", "isdir", "isfile", "list", "open", "stat"}
)
REQUIRED_WRITE_METHODS = frozenset({"makedirs", "mkdir", "remove", "rename"})

#: Probed by the patch layer (``_require()``, ``getattr``, ``hasattr``): the
#: corresponding stdlib call raises ``NotImplementedError`` when the backend
#: has no such method, rather than the patch failing to install.
OPTIONAL_READ_METHODS = frozenset(
    {
        "access",
        "getsize",
        "islink",
        "read",
        "readlink",
        "realpath",
        "resolve_path",
        "samefile",
    }
)
OPTIONAL_WRITE_METHODS = frozenset(
    {
        "chmod",
        "chown",
        "link",
        "replace",
        "rmdir",
        "symlink",
        "truncate",
        "utime",
        "write",
    }
)

#: Beyond the patch surface: methods callers reach for directly, such as
#: termish's shell over a filesystem. Not dispatched by any stdlib shim, but
#: composable — a wrapper that routes by path can forward them.
DIRECT_READ_METHODS = frozenset(
    {"get_metadata_snapshot", "glob", "invalidate", "lexists", "list_detailed"}
)
DIRECT_WRITE_METHODS = frozenset({"remove_many", "write_many"})

#: Storage-layout helpers: pure computation over a backend's own key scheme.
#: They read nothing, and they compose with nothing — a wrapper that routes by
#: path has no key scheme of its own to answer for, so it does not forward them.
KEY_SCHEME_METHODS = frozenset(
    {"is_metadata_key", "metadata_key", "path_for_metadata_key"}
)

#: A composing wrapper's own controls (``MountFS``). They re-point the
#: namespace, so they count as writes.
WRAPPER_WRITE_METHODS = frozenset({"mount", "unmount"})

REQUIRED_METHODS = REQUIRED_READ_METHODS | REQUIRED_WRITE_METHODS
OPTIONAL_METHODS = OPTIONAL_READ_METHODS | OPTIONAL_WRITE_METHODS

#: Everything the patch layer can dispatch to. The drift test in
#: ``tests/test_protocol.py`` asserts the patch layer reaches for nothing else.
DISPATCHED_METHODS = REQUIRED_METHODS | OPTIONAL_METHODS

#: What a path-routing wrapper forwards: every optional method plus the
#: composable direct-use ones. The required methods are implemented outright.
FORWARDED_METHODS = OPTIONAL_METHODS | DIRECT_READ_METHODS | DIRECT_WRITE_METHODS

READ_METHODS = (
    REQUIRED_READ_METHODS
    | OPTIONAL_READ_METHODS
    | DIRECT_READ_METHODS
    | KEY_SCHEME_METHODS
)
WRITE_METHODS = (
    REQUIRED_WRITE_METHODS
    | OPTIONAL_WRITE_METHODS
    | DIRECT_WRITE_METHODS
    | WRAPPER_WRITE_METHODS
)


@runtime_checkable
class FileSystem(Protocol):
    """Minimal interface for patch() patching.

    The methods declared in the class body are the required ones: the patch
    layer calls them unconditionally, so a backend without one cannot be
    patched. ``isinstance(fs, FileSystem)`` checks exactly these.

    Optional
    --------
    The patch layer also probes for the methods in ``OPTIONAL_METHODS``,
    reaching them through ``getattr`` rather than calling them outright. A
    backend that has none of them still patches; the stdlib function that
    needs one raises ``NotImplementedError`` when it is called:

    ``access``, ``chmod``, ``chown``, ``getsize``, ``islink``, ``link``,
    ``read``, ``readlink``, ``realpath``, ``replace``, ``resolve_path``,
    ``rmdir``, ``samefile``, ``symlink``, ``truncate``, ``utime``, ``write``.

    Beyond the patch surface, ``DIRECT_READ_METHODS`` and
    ``DIRECT_WRITE_METHODS`` name the methods callers use directly (``glob``,
    ``list_detailed``, ``write_many``, ...); no stdlib shim dispatches to
    them, but ``MountFS`` and ``ReadOnlyFS`` forward them.

    The module-level frozensets above are the machine-readable form of all of
    this, and the wrappers are built from them rather than from lists of
    their own.

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
