"""State-backed virtual filesystem implementation.

Provides VirtualFS and VirtualFile classes for file operations backed by
agent state, enabling file persistence and versioning.
"""

from __future__ import annotations

import base64
import errno
import fnmatch
import io
import os
import pickle
from collections.abc import MutableMapping
from datetime import datetime, timezone

from .base import FileInfo, FileMetadata


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


class VirtualFS:
    """State-backed virtual filesystem with metadata tracking.

    Provides file operations backed by agent state. Each file is stored
    as a separate state key, enabling granular versioning with Staged state.

    File metadata (size, creation time, modification time) is automatically
    tracked for all files and can be accessed via stat() or list_detailed().

    Files are stored as bytes. Text files are encoded as UTF-8.
    Directories are implicit (inferred from file paths, like S3).

    Example:
        >>> state = {}  # any MutableMapping[str, bytes]
        >>> vfs = VirtualFS(state)
        >>> vfs.write("data.csv", b"a,b,c\\n1,2,3")
        >>> vfs.read("data.csv")
        b'a,b,c\\n1,2,3'
        >>> vfs.list("/")
        ['data.csv']
        >>> meta = vfs.stat("data.csv")
        >>> print(f"Size: {meta.size} bytes, Created: {meta.created_at}")
    """

    PREFIX = "__vfs_"
    METADATA_KEY = "__vfs_metadata__"
    CWD_KEY = "__vfs_cwd__"

    def __init__(
        self,
        state: MutableMapping[str, bytes] | None = None,
        max_size_mb: int | None = None,
    ):
        """Initialize virtual filesystem backed by state.

        Args:
            state: State backend for file storage. Defaults to an empty dict.
            max_size_mb: Maximum total size of all files in megabytes.
                None means unlimited.
        """
        self._state = state if state is not None else {}
        self._dir_cache: set[str] | None = None
        self._max_size_bytes: int | None = (
            max_size_mb * 1024 * 1024 if max_size_mb is not None else None
        )
        self._current_size: int | None = None  # Lazy-computed from metadata

    # -------------------------------------------------------------------------
    # Working Directory
    # -------------------------------------------------------------------------

    def getcwd(self) -> str:
        """Get current working directory.

        Returns:
            Current working directory path (defaults to "/").
        """
        return self._state.get(self.CWD_KEY) or "/"

    def chdir(self, path: str) -> None:
        """Change current working directory.

        Args:
            path: Directory path to change to.

        Raises:
            FileNotFoundError: If directory doesn't exist.
        """
        resolved = self.resolve_path(path)
        # Use absolute path for isdir check to avoid double resolution
        absolute = "/" + resolved.lstrip("/")
        if not self.isdir(absolute):
            raise FileNotFoundError(f"No such directory: '{path}'")
        self._state[self.CWD_KEY] = absolute

    def glob(self, pattern: str) -> list[str]:
        """Return list of paths matching a glob pattern."""
        results = []
        cwd = self.getcwd()

        # If pattern is absolute, we match against full paths
        if pattern.startswith("/"):
            match_pattern = pattern.lstrip("/")
        else:
            if cwd == "/":
                match_pattern = pattern
            else:
                match_pattern = f"{cwd.lstrip('/')}/{pattern}"

        for key in self._state.keys():
            if (
                not self._is_vfs_key(key)
                or key == self.METADATA_KEY
                or key == self.CWD_KEY
            ):
                continue

            path = self._decode_path(
                key
            )  # normalized path e.g. "src/main.py" (no leading slash)

            # fnmatch against the full relative-to-root path
            if fnmatch.fnmatch(path, match_pattern):
                if pattern.startswith("/"):
                    # Return as absolute path (virtual)
                    results.append("/" + path)
                else:
                    # Return relative to CWD
                    if cwd == "/":
                        results.append(path)
                    elif path.startswith(cwd.lstrip("/") + "/"):
                        # cwd="/src" -> path="src/main.py"
                        # cwd.lstrip("/") + "/" -> "src/"
                        prefix_len = len(cwd.lstrip("/")) + 1
                        results.append(path[prefix_len:])

        return sorted(results)

    def resolve_path(self, path: str) -> str:
        """Resolve path (relative or absolute) against current working directory.

        Args:
            path: File or directory path (relative or absolute).

        Returns:
            Normalized absolute path.
        """
        if path.startswith("/"):
            return self._normalize_path(path)
        cwd = self.getcwd()
        return self._normalize_path(f"{cwd}/{path}")

    def _ensure_dir_cache(self) -> set[str]:
        """Lazy initialization of directory cache from state keys."""
        if self._dir_cache is not None:
            return self._dir_cache

        self._dir_cache = {"", "."}  # Root directories
        for key in self._state.keys():
            if key == self.METADATA_KEY or not self._is_vfs_key(key):
                continue

            try:
                path = self._decode_path(key)
                # Add all parent directories
                parts = path.lstrip("/").split("/")
                for i in range(len(parts)):
                    dir_path = "/".join(parts[:i])
                    self._dir_cache.add(dir_path)
                    self._dir_cache.add(dir_path + "/")
            except (KeyError, ValueError, UnicodeDecodeError):
                continue

        return self._dir_cache

    def _now_iso(self) -> str:
        """Get current UTC timestamp as ISO 8601 string with milliseconds."""
        return datetime.now(timezone.utc).isoformat()

    def _get_metadata(self) -> dict[str, FileMetadata]:
        """Load metadata dict from state.

        Returns:
            Dict mapping normalized paths to FileMetadata objects.
        """
        metadata_bytes = self._state.get(self.METADATA_KEY)
        if metadata_bytes is None:
            return {}
        return pickle.loads(metadata_bytes)

    def _set_metadata(self, metadata: dict[str, FileMetadata]) -> None:
        """Save metadata dict to state.

        Args:
            metadata: Dict mapping normalized paths to FileMetadata objects.
        """
        self._state[self.METADATA_KEY] = pickle.dumps(metadata)

    def _get_current_size(self) -> int:
        """Get total size of all files in the VFS.

        Computes from metadata on first call, then cached.
        Cache is invalidated on write/remove.

        Returns:
            Total size in bytes.
        """
        if self._current_size is not None:
            return self._current_size

        metadata = self._get_metadata()
        self._current_size = sum(
            m.size for m in metadata.values() if not getattr(m, "is_dir", False)
        )
        return self._current_size

    def _check_size_limit(self, path: str, new_content_size: int) -> None:
        """Check if adding content would exceed size limit.

        Args:
            path: File path being written.
            new_content_size: Size of new content in bytes.

        Raises:
            OSError: If write would exceed max_size_mb limit.
        """
        if self._max_size_bytes is None:
            return

        current = self._get_current_size()

        # Account for overwriting existing file
        metadata = self._get_metadata()
        normalized = self._normalize_path(path)
        existing_size = metadata.get(normalized, FileMetadata(0, "", "")).size

        new_total = current - existing_size + new_content_size

        if new_total > self._max_size_bytes:
            raise OSError(
                f"VFS size limit exceeded: {new_total / 1024 / 1024:.1f}MB > "
                f"{self._max_size_bytes / 1024 / 1024:.1f}MB"
            )

    def _update_file_metadata(self, path: str, size: int, is_new: bool) -> None:
        """Update metadata for a file (create or modify).

        Args:
            path: Normalized file path.
            size: File size in bytes.
            is_new: True if this is a new file, False if modifying existing.
        """
        metadata = self._get_metadata()
        now = self._now_iso()

        if is_new:
            # Metadata keys must be normalized to match _encode_path
            path = self._normalize_path(path)

        if is_new or path not in metadata:
            # New file - set both created_at and modified_at
            metadata[path] = FileMetadata(
                size=size,
                created_at=now,
                modified_at=now,
            )
        else:
            # Existing file - preserve created_at, update modified_at and size
            metadata[path] = FileMetadata(
                size=size,
                created_at=metadata[path].created_at,
                modified_at=now,
            )

        self._set_metadata(metadata)

    def get_metadata_snapshot(self) -> dict[str, FileMetadata]:
        """Get a copy of current file metadata for change detection.

        Used to compare before/after agent turns to detect file changes.

        Returns:
            Copy of metadata dict (safe to modify).
        """
        return self._get_metadata().copy()

    def _normalize_path(self, path: str) -> str:
        """Normalize file path for consistent internal keys.

        Args:
            path: File path (e.g., "./data.csv").

        Returns:
            Normalized path (e.g., "data.csv").
        """
        if not path or path in (".", "./", "/"):
            return "/"

        # Normalize path to canonical form
        # This handles ./a.py vs a.py, and a/./b vs a/b
        path = os.path.normpath(path).replace("\\", "/")

        # Remove leading slashes, handle empty/root
        path = path.lstrip("/") or "/"
        return path

    def _encode_path(self, path: str) -> str:
        """Convert file path to state key.

        Uses base32 encoding for safe, reversible path encoding.
        Paths are first resolved against the current working directory.

        Args:
            path: File path (e.g., "shared/data.csv" or relative like "file.txt").

        Returns:
            State key (e.g., "__vfs_ONQWIZI...").
        """
        # Resolve relative paths against CWD first
        path = self.resolve_path(path)
        path = self._normalize_path(path)
        encoded = base64.b32encode(path.encode()).decode().rstrip("=")
        return f"{self.PREFIX}{encoded}"

    def _decode_path(self, key: str) -> str:
        """Convert state key back to file path.

        Args:
            key: State key (e.g., "__vfs_ONQWIZI...").

        Returns:
            File path (e.g., "shared/data.csv").
        """
        encoded = key[len(self.PREFIX) :]
        # Add padding back
        padding = (8 - len(encoded) % 8) % 8
        encoded += "=" * padding
        return base64.b32decode(encoded).decode()

    def _is_vfs_key(self, key: str) -> bool:
        """Check if a state key is a VFS file."""
        return key.startswith(self.PREFIX)

    def open(
        self, path: str, mode: str = "r", **kwargs: object
    ) -> VirtualFile | io.BytesIO | io.StringIO:
        """Open a file, returning a file-like object.

        Args:
            path: File path to open.
            mode: File mode ('r', 'rb', 'w', 'wb', 'a', 'ab').
            **kwargs: Additional arguments (ignored for compatibility).

        Returns:
            File-like object for reading or writing.

        Raises:
            FileNotFoundError: If reading a file that doesn't exist.
            ValueError: If mode is invalid.
        """
        key = self._encode_path(path)

        if "r" in mode and "w" not in mode and "a" not in mode and "x" not in mode:
            # Read mode
            content = self._state.get(key)
            if content is None:
                raise FileNotFoundError(path)

            if "b" in mode:
                return io.BytesIO(content)
            else:
                return io.StringIO(content.decode("utf-8"))

        elif "w" in mode or "a" in mode or "x" in mode:
            # Write, append, or exclusive creation mode
            if "x" in mode and self.exists(path):
                raise FileExistsError(f"[Errno 17] File exists: '{path}'")

            # Validate parent directory exists (POSIX: open() fails with ENOENT)
            resolved = self.resolve_path(path)
            normalized = self._normalize_path(resolved)
            parent = "/".join(normalized.split("/")[:-1])
            if parent and not self.isdir("/" + parent):
                raise FileNotFoundError(f"No such file or directory: '{path}'")

            return VirtualFile(self, self._state, key, path, mode)

        else:
            raise ValueError(f"Invalid mode: {mode}")

    def read(self, path: str) -> bytes:
        """Read file contents as bytes.

        Args:
            path: File path to read.

        Returns:
            File contents as bytes.

        Raises:
            FileNotFoundError: If file doesn't exist.
        """
        key = self._encode_path(path)
        content = self._state.get(key)
        if content is None:
            raise FileNotFoundError(path)
        return content

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        """Write bytes to a file.

        Args:
            path: File path to write.
            content: Content to write (must be bytes).
            mode: Write mode ('w' for write/overwrite, 'a' for append).

        Raises:
            TypeError: If content is not bytes.
            OSError: If write would exceed max_size_mb limit.
        """
        if not isinstance(content, bytes):
            raise TypeError(f"Expected bytes, got {type(content).__name__}")

        # Auto-create parent directories
        resolved = self.resolve_path(path)
        normalized = self._normalize_path(resolved)
        parent = "/".join(normalized.split("/")[:-1])
        if parent and not self.isdir("/" + parent):
            self.makedirs("/" + parent)

        key = self._encode_path(path)

        # Handle append mode
        if mode == "a":
            try:
                existing = self.read(path)
                content = existing + content
            except FileNotFoundError:
                # If file doesn't exist, append behaves like write
                pass
        elif mode != "w":
            raise ValueError(f"Invalid mode: {mode}")

        # Check size limit before writing
        self._check_size_limit(path, len(content))

        # Check if file exists to determine if this is new or modified
        is_new = key not in self._state

        # Write content
        self._state[key] = content

        # Update metadata
        self._update_file_metadata(path, len(content), is_new)

        # Invalidate caches
        self._dir_cache = None
        self._current_size = None  # Will be recomputed on next access

    def write_many(self, files: dict[str, bytes]) -> None:
        """Write multiple files atomically.

        Args:
            files: Mapping of file path to content (bytes).

        Raises:
            TypeError: If any content is not bytes.
            OSError: If writes would exceed max_size_mb limit.

        Example:
            >>> vfs.write_many({
            ...     "data/file1.txt": b"content1",
            ...     "data/file2.txt": b"content2",
            ... })
        """
        # Validate all first
        for path, content in files.items():
            if not isinstance(content, bytes):
                raise TypeError(
                    f"Expected bytes for '{path}', got {type(content).__name__}"
                )

        # Check combined size limit before writing any files
        if self._max_size_bytes is not None:
            current = self._get_current_size()
            metadata = self._get_metadata()
            new_total = current

            for path, content in files.items():
                normalized = self._normalize_path(path)
                existing_size = metadata.get(normalized, FileMetadata(0, "", "")).size
                new_total = new_total - existing_size + len(content)

            if new_total > self._max_size_bytes:
                raise OSError(
                    f"VFS size limit exceeded: {new_total / 1024 / 1024:.1f}MB > "
                    f"{self._max_size_bytes / 1024 / 1024:.1f}MB"
                )

        # Write all files and update metadata
        for path, content in files.items():
            key = self._encode_path(path)
            is_new = key not in self._state
            self._state[key] = content
            self._update_file_metadata(path, len(content), is_new)

        # Invalidate caches
        self._dir_cache = None
        self._current_size = None  # Will be recomputed on next access

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        """List directory contents.

        Returns children of the directory (files and subdirectories).
        Directories are implicit (inferred from file paths).

        Args:
            path: Directory path to list.
            recursive: If True, list all nested files and directories.

        Returns:
            List of file/directory names in the directory.
        """
        # Validate before resolving (isfile/isdir resolve internally)
        if self.isfile(path):
            raise NotADirectoryError(f"Not a directory: '{path}'")
        if not self.isdir(path):
            raise FileNotFoundError(f"No such directory: '{path}'")

        # Resolve path against CWD first, then normalize
        path = self.resolve_path(path)

        # Adjust logic to match original list expectation (empty string for root)
        if path == "." or path == "/":
            path = ""
        else:
            path = path + "/"

        results: set[str] = set()
        for key in self._state.keys():
            # Skip metadata and CWD keys
            if key == self.METADATA_KEY or key == self.CWD_KEY:
                continue

            if not self._is_vfs_key(key):
                continue

            file_path = self._decode_path(key)
            file_path = file_path.lstrip("/")

            if path and not file_path.startswith(path):
                continue

            # Get the remainder after the directory prefix
            remainder = file_path[len(path) :]
            if not remainder:
                continue

            if recursive:
                # Add all intermediate directory parts too
                parts = remainder.split("/")
                for i in range(1, len(parts) + 1):
                    results.add("/".join(parts[:i]))
            else:
                # Get immediate child (first path component)
                if "/" in remainder:
                    results.add(remainder.split("/")[0])  # Subdirectory
                else:
                    results.add(remainder)  # File

        # Include explicit directories from metadata
        metadata = self._get_metadata()
        for dir_path, meta in metadata.items():
            if not meta.is_dir:
                continue
            dir_path = dir_path.lstrip("/")
            if path and not dir_path.startswith(path):
                continue
            remainder = dir_path[len(path) :]
            if not remainder:
                continue
            if recursive:
                parts = remainder.split("/")
                for i in range(1, len(parts) + 1):
                    results.add("/".join(parts[:i]))
            else:
                child = remainder.split("/")[0]
                results.add(child)

        return sorted(results)

    def exists(self, path: str) -> bool:
        """Check if a file or directory exists.

        For files, checks if the exact path exists.
        For directories, checks explicit entries or implicit presence.

        Args:
            path: Path to check.

        Returns:
            True if path exists, False otherwise.
        """
        # Check for exact file match
        key = self._encode_path(path)
        if key in self._state:
            return True

        # Check for explicit directory entry in metadata
        resolved = self.resolve_path(path)
        normalized = self._normalize_path(resolved)
        metadata = self._get_metadata()
        if normalized in metadata and metadata[normalized].is_dir:
            return True

        # Check for implicit directory match (backward compat)
        if normalized == "/":
            normalized = ""
        cache = self._ensure_dir_cache()
        return normalized in cache or (normalized + "/") in cache

    def isfile(self, path: str) -> bool:
        """Check if path is a file.

        Args:
            path: Path to check.

        Returns:
            True if path is a file, False otherwise.
        """
        key = self._encode_path(path)
        return key in self._state

    def isdir(self, path: str) -> bool:
        """Check if path is a directory.

        Checks for explicit directory entries in metadata first,
        then falls back to implicit directory detection (any path with files underneath).

        Args:
            path: Path to check.

        Returns:
            True if path is a directory, False otherwise.
        """
        # Resolve path against CWD first
        path = self.resolve_path(path)
        normalized = self._normalize_path(path)

        # Root is always a directory
        if normalized in ("", "/"):
            return True

        # Check for explicit directory entry in metadata
        metadata = self._get_metadata()
        if normalized in metadata and metadata[normalized].is_dir:
            return True

        # Fall back to implicit detection (for backward compatibility)
        cache = self._ensure_dir_cache()
        return normalized in cache or (normalized + "/") in cache

    def islink(self, path: str) -> bool:
        """Check if path is a symbolic link.

        VFS does not currently support symbolic links.

        Args:
            path: Path to check.

        Returns:
            Always False.
        """
        return False

    def lexists(self, path: str) -> bool:
        """Check if path exists (without following symlinks).

        Since VFS has no symlinks, this is same as exists().

        Args:
            path: Path to check.

        Returns:
            True if path exists, False otherwise.
        """
        return self.exists(path)

    def samefile(self, path1: str, path2: str) -> bool:
        """Check if two paths refer to the same file.

        Args:
            path1: First path.
            path2: Second path.

        Returns:
            True if paths normalize to the same VFS key and exist.
        """
        if not (self.exists(path1) and self.exists(path2)):
            return False
        return self._normalize_path(self.resolve_path(path1)) == self._normalize_path(
            self.resolve_path(path2)
        )

    def realpath(self, path: str) -> str:
        """Return the canonical path.

        For VFS, this is the normalized absolute path.

        Args:
            path: Path to resolve.

        Returns:
            Canonical path string.
        """
        return "/" + self._normalize_path(self.resolve_path(path)).lstrip("/")

    def getsize(self, path: str) -> int:
        """Get file size in bytes.

        Args:
            path: File path.

        Returns:
            Size in bytes.

        Raises:
            FileNotFoundError: If file doesn't exist.
        """
        content = self.read(path)
        return len(content)

    def remove(self, path: str) -> None:
        """Remove a file.

        Args:
            path: File path to remove.

        Raises:
            FileNotFoundError: If file doesn't exist.
        """
        key = self._encode_path(path)
        if key not in self._state:
            raise FileNotFoundError(path)
        del self._state[key]

        # Remove from metadata
        path = self._normalize_path(path)
        metadata = self._get_metadata()
        metadata.pop(path, None)
        self._set_metadata(metadata)

        # Invalidate caches
        self._dir_cache = None
        self._current_size = None  # Will be recomputed on next access

    def remove_many(self, paths: list[str]) -> None:
        """Remove multiple files.

        Args:
            paths: List of file paths to remove.

        Raises:
            FileNotFoundError: If a file doesn't exist.
        """
        for path in paths:
            self.remove(path)

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory.

        Args:
            path: Directory path.
            exist_ok: If True, don't raise if directory exists.
            parents: If True, create parent directories as needed.

        Raises:
            FileExistsError: If path exists (as file or dir when exist_ok=False).
            FileNotFoundError: If parent doesn't exist and parents=False.
        """
        if parents:
            self.makedirs(path, exist_ok=exist_ok)
            return

        path = self.resolve_path(path)
        normalized = self._normalize_path(path)

        # Validate parent exists
        parent = "/".join(normalized.split("/")[:-1])
        if parent and not self.isdir("/" + parent):
            raise FileNotFoundError(f"No such file or directory: '{path}'")

        # Check if already exists
        if self.isfile(path):
            raise FileExistsError(f"File exists: {path}")
        if self.isdir(path):
            if exist_ok:
                return
            raise FileExistsError(f"Directory exists: {path}")

        # Create directory metadata entry
        now = datetime.now(timezone.utc).isoformat()
        metadata = self._get_metadata()
        metadata[normalized] = FileMetadata(
            size=0,
            created_at=now,
            modified_at=now,
            is_dir=True,
        )
        self._set_metadata(metadata)
        self._dir_cache = None  # Invalidate cache

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Create directory tree.

        Creates all parent directories as needed.

        Args:
            path: Directory path.
            exist_ok: If True, don't raise if directory exists.

        Raises:
            FileExistsError: If path exists as a file.
        """
        path = self.resolve_path(path)
        parts = path.strip("/").split("/")

        # Create each parent directory
        for i in range(len(parts)):
            dir_path = "/" + "/".join(parts[: i + 1])
            if self.isfile(dir_path):
                raise FileExistsError(f"File exists: {dir_path}")
            if not self.isdir(dir_path):
                self.mkdir(dir_path, exist_ok=True)

    def rmdir(self, path: str) -> None:
        """Remove an empty directory.

        Args:
            path: Directory path to remove.

        Raises:
            FileNotFoundError: If directory doesn't exist.
            NotADirectoryError: If path is a file.
            OSError: If directory is not empty.
        """
        path = self.resolve_path(path)
        normalized = self._normalize_path(path)

        if not self.exists(path):
            raise FileNotFoundError(f"No such directory: {path}")
        if self.isfile(path):
            raise NotADirectoryError(f"Not a directory: {path}")
        if not self.isdir(path):
            raise FileNotFoundError(f"No such directory: {path}")

        # Check if directory is empty
        children = self.list(path)
        if children:
            raise OSError(f"Directory not empty: {path}")

        # Remove directory metadata
        metadata = self._get_metadata()
        metadata.pop(normalized, None)
        self._set_metadata(metadata)
        self._dir_cache = None

    def rename(self, src: str, dst: str) -> None:
        """Rename/move a file or directory.

        Args:
            src: Source path (file or directory).
            dst: Destination path.

        Raises:
            FileNotFoundError: If source doesn't exist.
        """
        src_resolved = self.resolve_path(src)
        dst_resolved = self.resolve_path(dst)
        src_norm = self._normalize_path(src_resolved)
        dst_norm = self._normalize_path(dst_resolved)

        if self.isfile(src):
            # File rename
            content = self.read(src)
            metadata = self._get_metadata()
            src_meta = metadata.get(src_norm)

            self.write(dst, content)

            # Preserve created_at from source
            metadata = self._get_metadata()
            if src_meta:
                dst_meta = metadata[dst_norm]
                metadata[dst_norm] = FileMetadata(
                    size=dst_meta.size,
                    created_at=src_meta.created_at,
                    modified_at=dst_meta.modified_at,
                )
                self._set_metadata(metadata)

            self.remove(src)

        elif self.isdir(src):
            # Directory rename — move all children
            src_prefix = src_norm.rstrip("/") + "/"

            # Collect all files under src
            files_to_move = []
            for key in list(self._state.keys()):
                if not self._is_vfs_key(key):
                    continue
                if key == self.METADATA_KEY or key == self.CWD_KEY:
                    continue
                file_path = self._decode_path(key).lstrip("/")
                if file_path == src_norm or file_path.startswith(src_prefix):
                    files_to_move.append((key, file_path))

            metadata = self._get_metadata()
            for key, file_path in files_to_move:
                # Compute new path
                rel = file_path[len(src_norm) :]
                new_path = dst_norm + rel
                new_key = self._encode_path("/" + new_path)

                # Move content
                self._state[new_key] = self._state.pop(key)

                # Move metadata
                if file_path in metadata:
                    metadata[new_path] = metadata.pop(file_path)

            # Move directory metadata entries
            dir_keys_to_move = [
                k for k in metadata if k == src_norm or k.startswith(src_prefix)
            ]
            for k in dir_keys_to_move:
                rel = k[len(src_norm) :]
                metadata[dst_norm + rel] = metadata.pop(k)

            self._set_metadata(metadata)
            self._dir_cache = None
        else:
            raise FileNotFoundError(src)

    def replace(self, src: str, dst: str) -> None:
        """Replace dst with src (alias for rename)."""
        self.rename(src, dst)

    def readlink(self, path: str) -> str:
        """Read a symbolic link (not supported in VFS)."""
        raise OSError(errno.EINVAL, "Not a symbolic link", path)

    def symlink(self, src: str, dst: str) -> None:
        """Create a symbolic link (not supported in VFS)."""
        raise OSError(errno.EPERM, "VirtualFS does not support symlinks")

    def chmod(self, path: str, mode: int) -> None:
        """Change file mode (no-op for VFS)."""
        if not self.exists(path):
            raise FileNotFoundError(f"No such file or directory: {path}")

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Change file owner (no-op for VFS)."""
        if not self.exists(path):
            raise FileNotFoundError(f"No such file or directory: {path}")

    def access(self, path: str, mode: int) -> bool:
        """Check file access (returns exists() for VFS)."""
        return self.exists(path)

    def link(self, src: str, dst: str) -> None:
        """Create a hard link (copies content in VFS)."""
        content = self.read(src)
        self.write(dst, content)

    def truncate(self, path: str, length: int) -> None:
        """Truncate file to given length."""
        content = self.read(path)
        self.write(path, content[:length])

    def stat(self, path: str) -> FileMetadata:
        """Get metadata for a specific file or directory.

        Args:
            path: File or directory path.

        Returns:
            FileMetadata object with size and timestamps.

        Raises:
            FileNotFoundError: If path doesn't exist.

        Example:
            >>> meta = vfs.stat("data.csv")
            >>> print(f"Size: {meta.size} bytes")
            >>> print(f"Created: {meta.created_at}")
        """
        # Check for file first
        if self.isfile(path):
            path = self._normalize_path(path)
            metadata = self._get_metadata()
            return metadata[path]

        # Check for directory
        if self.isdir(path):
            normalized = self._normalize_path(path)
            metadata = self._get_metadata()
            if normalized in metadata:
                return metadata[normalized]
            now = datetime.now(timezone.utc).isoformat()
            return FileMetadata(size=0, created_at=now, modified_at=now, is_dir=True)

        raise FileNotFoundError(path)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[FileInfo]:
        """List directory contents with full file metadata.

        Returns FileInfo objects for each file and subdirectory with complete
        metadata (size, timestamps). Useful for UI file viewers.

        Args:
            path: Directory path to list (default: root).
            recursive: If True, list all nested files and directories.

        Returns:
            List of FileInfo objects sorted by name.

        Example:
            >>> files = vfs.list_detailed("/shared")
            >>> for f in files:
            ...     print(f"{f.name:20} {f.size:>10} {f.modified_at}")
        """
        # Get file list from existing list() method
        names = self.list(path, recursive=recursive)

        # Normalize path similar to list() methods to build correct full paths
        normalized_path = path.strip()
        if normalized_path == "." or normalized_path == "./":
            normalized_path = ""
        else:
            normalized_path = normalized_path.strip("/")

        # Load all metadata once
        all_metadata = self._get_metadata()

        # Build FileInfo objects
        result = []
        for name in names:
            # Construct full path
            if not normalized_path:
                full_path = name
            else:
                full_path = f"{normalized_path}/{name}"

            # Check if it's a directory
            is_dir = self.isdir(full_path)

            if is_dir:
                result.append(
                    FileInfo(
                        name=name,
                        path=full_path,
                        size=0,
                        created_at=self._now_iso(),
                        modified_at=self._now_iso(),
                        is_dir=True,
                    )
                )
            else:
                # File - get metadata
                meta = all_metadata.get(full_path)
                if meta:
                    result.append(
                        FileInfo(
                            name=name,
                            path=full_path,
                            size=meta.size,
                            created_at=meta.created_at,
                            modified_at=meta.modified_at,
                            is_dir=False,
                        )
                    )
                else:
                    # File exists but has no metadata
                    content = self.read(full_path)
                    now = self._now_iso()
                    result.append(
                        FileInfo(
                            name=name,
                            path=full_path,
                            size=len(content),
                            created_at=now,
                            modified_at=now,
                            is_dir=False,
                        )
                    )

        return result
