"""State-backed virtual filesystem implementation."""

from __future__ import annotations

import base64
import errno
import fnmatch
import io
import json
import os
from collections.abc import MutableMapping
from datetime import datetime, timezone

from .base import FileInfo, FileMetadata
from .virtualfile import VirtualFile


class VirtualFS:
    """State-backed virtual filesystem with metadata tracking.

    Provides file operations backed by agent state. Each file is stored
    as a separate state key, enabling granular versioning with Staged state.

    File metadata (size, creation time, modification time) is automatically
    tracked for all files and can be accessed via stat() or list_detailed().

    Files are stored as bytes. Text files are encoded as UTF-8.
    Directories are implicit (inferred from file paths, like S3).

    Metadata is stored one row per path, beside the blob it describes:
    a file at ``data.csv`` has its bytes under ``__vfs_<encoded>`` and its
    ``FileMetadata`` under ``__vfs_meta_<encoded>``, with the same encoding
    in both. Use ``metadata_key()``, ``is_metadata_key()`` and
    ``path_for_metadata_key()`` to move between the two rather than
    reproducing the encoding. Directories created with ``mkdir()`` get a
    row of their own (``is_dir=True``); implicit directories have none.

    Migration from the single table: monkeyfs 0.1.9 and earlier kept every
    path's metadata in one JSON blob under ``__vfs_metadata__``. A state
    written by those versions still reads correctly -- the table is
    consulted for any path that has no row -- and is drained write by
    write: writing a path stores its row and removes its table entry, and
    the table key is deleted once the last entry is gone. Nothing migrates
    on open, because a read must not write.

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
    META_PREFIX = "__vfs_meta_"
    CWD_KEY = "__vfs_cwd__"

    # Deprecated: the single metadata table written by 0.1.9 and earlier.
    # Metadata now lives in one row per path under META_PREFIX. The name
    # stays so consumers that still reach for the old key can name it
    # while they migrate; nothing here writes it except to drain it.
    # No blob or row key can collide with it: blob and row encodings are
    # uppercase base32, and "__vfs_metadata__" does not start with
    # META_PREFIX (the character after "__vfs_meta" is "d", not "_").
    METADATA_KEY = "__vfs_metadata__"

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
        # Rows read so far, keyed by canonical path. A None value is a
        # cached miss: no row, and no legacy table entry either.
        self._meta_cache: dict[str, FileMetadata | None] = {}
        # Every path's metadata, built by one full scan of the state.
        # None until something needs the whole set.
        self._all_meta: dict[str, FileMetadata] | None = None
        # The legacy __vfs_metadata__ table, parsed on demand.
        self._legacy_table: dict[str, FileMetadata] | None = None
        self._max_size_bytes: int | None = (
            max_size_mb * 1024 * 1024 if max_size_mb is not None else None
        )
        self._current_size: int | None = None  # Lazy-computed from metadata

    def invalidate(self) -> None:
        """Drop all lazy caches so subsequent reads hit the state backend.

        Call this after the backing state has been mutated externally
        (e.g. a versioned store rolled back or reset underneath this
        instance). The VirtualFS itself invalidates on writes made
        through it; it cannot see writes made around it.
        """
        self._dir_cache = None
        self._meta_cache = {}
        self._all_meta = None
        self._legacy_table = None
        self._current_size = None

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
            if not self._is_vfs_key(key):
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
            if not self._is_vfs_key(key):
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

    # -------------------------------------------------------------------------
    # Metadata rows
    #
    # One row per path, keyed beside the blob it describes. The rule the
    # layer above depends on: a row is written whenever, and only when, its
    # blob is written or its metadata changes. Nothing else touches a row.
    # That is what lets a key-level three-way merge of blobs and rows stay
    # consistent -- two branches that write different files touch disjoint
    # keys, so neither the blobs nor the rows conflict.
    # -------------------------------------------------------------------------

    def metadata_key(self, path: str) -> str:
        """State key holding the metadata row for ``path``.

        The row is the blob key with ``PREFIX`` swapped for
        ``META_PREFIX``, so a row and its blob are siblings under one
        encoding. ``path`` is resolved against the CWD, exactly as
        ``_encode_path()`` resolves it.
        """
        return self.META_PREFIX + self._encode_path(path)[len(self.PREFIX) :]

    @classmethod
    def is_metadata_key(cls, key: str) -> bool:
        """True if ``key`` holds a metadata row rather than file content."""
        return key.startswith(cls.META_PREFIX)

    @classmethod
    def path_for_metadata_key(cls, key: str) -> str:
        """The root-relative path whose metadata ``key`` holds.

        Raises:
            ValueError: If ``key`` is not a metadata row key.
        """
        if not cls.is_metadata_key(key):
            raise ValueError(f"Not a metadata row key: {key!r}")
        return cls._decode_path(cls.PREFIX + key[len(cls.META_PREFIX) :])

    def _canonical_path(self, path: str) -> str:
        """Path in the one form blob keys and metadata rows agree on.

        Resolved against the CWD and normalized. Blob keys encode the
        resolved path, so rows must be keyed resolved too — otherwise one
        file holds two rows depending on which form each caller passed.
        """
        return self._normalize_path(self.resolve_path(path))

    def _row_key(self, canonical: str) -> str:
        """Row key for an already-canonical path.

        Canonical paths carry no leading slash, so the slash goes back on
        before encoding — otherwise the CWD would be applied a second
        time and the row would land under the wrong key.
        """
        return self.metadata_key("/" + canonical.lstrip("/"))

    @staticmethod
    def _row_fields(meta: FileMetadata) -> dict[str, object]:
        """The JSON body of a metadata row."""
        return {
            "size": meta.size,
            "created_at": meta.created_at,
            "modified_at": meta.modified_at,
            "is_dir": meta.is_dir,
        }

    def _legacy(self) -> dict[str, FileMetadata]:
        """The legacy ``__vfs_metadata__`` table, parsed on demand.

        Empty for any state written by this version once migration has
        finished, and for every state that never held the table.
        """
        if self._legacy_table is not None:
            return self._legacy_table

        raw = self._state.get(self.METADATA_KEY)
        table: dict[str, FileMetadata] = {}
        if raw is not None:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                for path, fields in parsed.items():
                    if isinstance(fields, dict):
                        try:
                            table[path] = FileMetadata(**fields)
                        except TypeError:
                            continue
        self._legacy_table = table
        return table

    def _legacy_forget(self, path: str) -> None:
        """Drop one path from the legacy table; delete the key when empty.

        Migration is write-driven: a read never rewrites state, so an old
        table is drained one path at a time as writes land on those paths,
        and the key disappears once the last entry has a row.
        """
        table = self._legacy()
        if path not in table:
            return

        del table[path]
        if self._all_meta is not None:
            self._all_meta.pop(path, None)

        if table:
            self._state[self.METADATA_KEY] = json.dumps(
                {p: self._row_fields(m) for p, m in table.items()}
            ).encode()
        elif self.METADATA_KEY in self._state:
            del self._state[self.METADATA_KEY]

    def _meta_at(self, canonical: str) -> FileMetadata | None:
        """Metadata for an already-canonical path: its row, else the table."""
        # Once the full set has been built it is authoritative — writes keep
        # it in step — so answer from it rather than reading the row again.
        if self._all_meta is not None:
            return self._all_meta.get(canonical)
        if canonical in self._meta_cache:
            return self._meta_cache[canonical]

        raw = self._state.get(self._row_key(canonical))
        meta: FileMetadata | None = None
        if raw is not None:
            try:
                fields = json.loads(raw)
                meta = FileMetadata(**fields)
            except (TypeError, ValueError):
                meta = None
        if meta is None:
            meta = self._legacy().get(canonical)

        self._meta_cache[canonical] = meta
        return meta

    def _meta_get(self, path: str) -> FileMetadata | None:
        """Metadata for a caller-supplied path, tolerating legacy rows.

        Prefers the canonical row; falls back to the raw normalized key
        for table entries written before keys resolved (0.1.8 and
        earlier), so old state keeps working instead of orphaning.
        """
        canonical = self._canonical_path(path)
        meta = self._meta_at(canonical)
        if meta is not None:
            return meta
        unresolved = self._normalize_path(path)
        if unresolved != canonical:
            return self._legacy().get(unresolved)
        return None

    def _meta_put(self, canonical: str, meta: FileMetadata) -> None:
        """Write the metadata row for an already-canonical path."""
        self._legacy_forget(canonical)
        self._state[self._row_key(canonical)] = json.dumps(
            self._row_fields(meta)
        ).encode()
        self._meta_cache[canonical] = meta
        if self._all_meta is not None:
            self._all_meta[canonical] = meta

    def _meta_drop(self, canonical: str) -> None:
        """Remove the metadata row for an already-canonical path."""
        self._legacy_forget(canonical)
        key = self._row_key(canonical)
        if key in self._state:
            del self._state[key]
        self._meta_cache[canonical] = None
        if self._all_meta is not None:
            self._all_meta.pop(canonical, None)

    def _meta_set(self, path: str, meta: FileMetadata) -> None:
        """Write the metadata row for a caller-supplied path.

        Drains the legacy table under both the canonical and the
        unresolved key, so an old entry cannot survive as a second row
        for the file that just moved to its own key.
        """
        canonical = self._canonical_path(path)
        self._legacy_forget(self._normalize_path(path))
        self._meta_put(canonical, meta)

    def _meta_delete(self, path: str) -> None:
        """Remove the metadata row for a caller-supplied path."""
        canonical = self._canonical_path(path)
        self._legacy_forget(self._normalize_path(path))
        self._meta_drop(canonical)

    def _meta_all(self) -> dict[str, FileMetadata]:
        """Every path's metadata: the rows, plus the legacy table for the rest.

        Scans the state, so callers that need one path should use
        ``_meta_get()``. Cached until ``invalidate()``; writes made
        through this instance keep the cache in step.
        """
        if self._all_meta is not None:
            return self._all_meta

        rows: dict[str, FileMetadata] = {}
        for key in list(self._state.keys()):
            if not self.is_metadata_key(key):
                continue
            raw = self._state.get(key)
            if raw is None:
                continue
            try:
                path = self.path_for_metadata_key(key)
                rows[self._normalize_path(path)] = FileMetadata(**json.loads(raw))
            except (TypeError, ValueError, UnicodeDecodeError):
                # An undecodable key or an unreadable body is not a row.
                continue

        for path, meta in self._legacy().items():
            rows.setdefault(path, meta)

        self._all_meta = rows
        return rows

    def _get_current_size(self) -> int:
        """Get total size of all files in the VFS.

        Summed from the metadata rows on first call, then cached.
        Cache is invalidated on write/remove.

        Returns:
            Total size in bytes.
        """
        if self._current_size is not None:
            return self._current_size

        self._current_size = sum(
            m.size for m in self._meta_all().values() if not m.is_dir
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
        entry = self._meta_get(path)
        existing_size = entry.size if entry is not None else 0

        new_total = current - existing_size + new_content_size

        if new_total > self._max_size_bytes:
            raise OSError(
                f"VFS size limit exceeded: {new_total / 1024 / 1024:.1f}MB > "
                f"{self._max_size_bytes / 1024 / 1024:.1f}MB"
            )

    def _update_file_metadata(self, path: str, size: int, is_new: bool) -> None:
        """Update metadata for a file (create or modify).

        Args:
            path: File path (relative or absolute).
            size: File size in bytes.
            is_new: True if this is a new file, False if modifying existing.
        """
        now = self._now_iso()
        existing = None if is_new else self._meta_get(path)
        self._meta_set(
            path,
            FileMetadata(
                size=size,
                created_at=existing.created_at if existing is not None else now,
                modified_at=now,
            ),
        )

    def get_metadata_snapshot(self) -> dict[str, FileMetadata]:
        """Get a copy of current file metadata for change detection.

        Merges the per-path rows with any entries still left in a legacy
        ``__vfs_metadata__`` table, so a half-migrated state reports every
        path exactly once, the row winning where both hold one.

        Returns:
            Copy of metadata dict (safe to modify).
        """
        return dict(self._meta_all())

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
        # On Windows, normpath produces backslashes; replace with forward
        # slashes so VFS keys are consistent across platforms.
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

    @classmethod
    def _decode_path(cls, key: str) -> str:
        """Convert state key back to file path.

        Args:
            key: State key (e.g., "__vfs_ONQWIZI...").

        Returns:
            File path (e.g., "shared/data.csv").
        """
        encoded = key[len(cls.PREFIX) :]
        # Add padding back
        padding = (8 - len(encoded) % 8) % 8
        encoded += "=" * padding
        return base64.b32decode(encoded).decode()

    @classmethod
    def _is_vfs_key(cls, key: str) -> bool:
        """Check if a state key holds file content.

        A metadata row is not a file, and neither is the CWD slot or the
        legacy metadata table, so every scan that enumerates files skips
        all three here rather than each remembering the list.
        """
        return (
            key.startswith(cls.PREFIX)
            and not cls.is_metadata_key(key)
            and key not in (cls.METADATA_KEY, cls.CWD_KEY)
        )

    def open(
        self, path: str, mode: str = "r", **kwargs: object
    ) -> VirtualFile | io.BytesIO | io.StringIO:
        """Open a file, returning a file-like object.

        Args:
            path: File path to open.
            mode: File mode ('r', 'rb', 'w', 'wb', 'a', 'ab', 'r+', 'rb+').
            **kwargs: Additional arguments (ignored for compatibility).

        Returns:
            File-like object for reading or writing.

        Raises:
            FileNotFoundError: If reading a file that doesn't exist.
            ValueError: If mode is invalid.
        """
        key = self._encode_path(path)

        if (
            "r" in mode
            and "+" not in mode
            and "w" not in mode
            and "a" not in mode
            and "x" not in mode
        ):
            # Read mode
            content = self._state.get(key)
            if content is None:
                raise FileNotFoundError(path)

            if "b" in mode:
                return io.BytesIO(content)
            else:
                return io.StringIO(content.decode("utf-8"))

        elif "w" in mode or "a" in mode or "x" in mode or ("r" in mode and "+" in mode):
            # Write, append, or exclusive creation mode
            if "r" in mode and "+" in mode and self._state.get(key) is None:
                raise FileNotFoundError(path)

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
            new_total = current

            for path, content in files.items():
                entry = self._meta_get(path)
                existing_size = entry.size if entry is not None else 0
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

        # Include explicit directories from their metadata rows
        for dir_path, meta in self._meta_all().items():
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

        # Check for an explicit directory row. Existence probes land on
        # paths that hold nothing, so this reads the whole set rather than
        # caching a miss per path asked about.
        normalized = self._canonical_path(path)
        meta = self._meta_all().get(normalized)
        if meta is not None and meta.is_dir:
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
        normalized = self._canonical_path(path)

        # Root is always a directory
        if normalized in ("", "/"):
            return True

        # Check for an explicit directory row. Like exists(), this is asked
        # about paths that hold nothing, so it reads the whole set rather
        # than caching a miss per path asked about.
        meta = self._meta_all().get(normalized)
        if meta is not None and meta.is_dir:
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

        # The row goes with the blob; a legacy table entry for the same
        # path must not survive its file either.
        self._meta_delete(path)

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
        # Delete from backing state
        for path in paths:
            key = self._encode_path(path)
            if key not in self._state:
                raise FileNotFoundError(path)
            del self._state[key]

        # One row removed per path, matching the blobs just deleted
        for path in paths:
            self._meta_delete(path)

        # Invalidate caches
        self._dir_cache = None
        self._current_size = None

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

        # Create the directory's own row (implicit directories have none)
        now = datetime.now(timezone.utc).isoformat()
        self._meta_put(
            normalized,
            FileMetadata(size=0, created_at=now, modified_at=now, is_dir=True),
        )
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

        # Remove the directory's row
        self._meta_drop(normalized)
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
            src_meta = self._meta_at(src_norm)

            self.write(dst, content)

            # Preserve created_at from source
            if src_meta is not None:
                dst_meta = self._meta_at(dst_norm)
                if dst_meta is not None:
                    self._meta_put(
                        dst_norm,
                        FileMetadata(
                            size=dst_meta.size,
                            created_at=src_meta.created_at,
                            modified_at=dst_meta.modified_at,
                        ),
                    )

            self.remove(src)

        elif self.isdir(src):
            # Directory rename — move all children
            src_prefix = src_norm.rstrip("/") + "/"

            # Collect all files under src
            files_to_move = []
            for key in list(self._state.keys()):
                if not self._is_vfs_key(key):
                    continue
                file_path = self._decode_path(key).lstrip("/")
                if file_path == src_norm or file_path.startswith(src_prefix):
                    files_to_move.append((key, file_path))

            for key, file_path in files_to_move:
                # Compute new path
                rel = file_path[len(src_norm) :]
                new_path = dst_norm + rel
                new_key = self._encode_path("/" + new_path)

                # Move content
                self._state[new_key] = self._state.pop(key)

            # Every row under the tree follows its blob, directory rows
            # included. Snapshotted first: writing rows mutates the map
            # this is reading.
            rows_to_move = [
                (path, meta)
                for path, meta in self._meta_all().items()
                if path == src_norm or path.startswith(src_prefix)
            ]
            for path, meta in rows_to_move:
                self._meta_drop(path)
                self._meta_put(dst_norm + path[len(src_norm) :], meta)

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
            entry = self._meta_get(path)
            if entry is not None:
                return entry
            now = datetime.now(timezone.utc).isoformat()
            return FileMetadata(
                size=self.getsize(path), created_at=now, modified_at=now
            )

        # Check for directory
        if self.isdir(path):
            entry = self._meta_get(path)
            if entry is not None:
                return entry
            now = datetime.now(timezone.utc).isoformat()
            return FileMetadata(size=0, created_at=now, modified_at=now, is_dir=True)

        raise FileNotFoundError(path)

    def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
    ) -> None:
        """Update access and modification times for a file or directory.

        Args:
            path: File or directory path.
            times: (atime, mtime) as float timestamps. If None, uses current time.

        Raises:
            FileNotFoundError: If path doesn't exist.
        """
        if not self.exists(path):
            raise FileNotFoundError(path)

        old = self._meta_get(path)

        if times is not None:
            mtime = datetime.fromtimestamp(times[1], tz=timezone.utc).isoformat()
        else:
            mtime = self._now_iso()

        if old is not None:
            updated = FileMetadata(
                size=old.size,
                created_at=old.created_at,
                modified_at=mtime,
                is_dir=old.is_dir,
            )
        else:
            updated = FileMetadata(
                size=0,
                created_at=mtime,
                modified_at=mtime,
                is_dir=self.isdir(path),
            )

        self._meta_set(path, updated)

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
        user_prefix = path.rstrip("/")

        # Children are named relative to the queried directory, so the
        # directory is canonicalized once here and every lookup below is
        # absolute — a relative name resolved against the CWD instead
        # would miss the row whenever the two differ.
        base = self._canonical_path(path)
        if base == "/":
            base = ""

        # Build FileInfo objects
        result = []
        for name in names:
            internal_path = f"{base}/{name}" if base else name
            absolute = "/" + internal_path

            # Display path preserves the user's queried prefix
            display = f"{user_prefix}/{name}" if user_prefix != "." else name

            meta = self._meta_at(internal_path)

            # Check if it's a directory
            if self.isdir(absolute):
                now = self._now_iso()
                result.append(
                    FileInfo(
                        name=name,
                        path=display,
                        size=0,
                        created_at=meta.created_at if meta is not None else now,
                        modified_at=meta.modified_at if meta is not None else now,
                        is_dir=True,
                    )
                )
            elif meta is not None:
                result.append(
                    FileInfo(
                        name=name,
                        path=display,
                        size=meta.size,
                        created_at=meta.created_at,
                        modified_at=meta.modified_at,
                        is_dir=False,
                    )
                )
            else:
                # File exists but has no row
                content = self.read(absolute)
                now = self._now_iso()
                result.append(
                    FileInfo(
                        name=name,
                        path=display,
                        size=len(content),
                        created_at=now,
                        modified_at=now,
                        is_dir=False,
                    )
                )

        return result
