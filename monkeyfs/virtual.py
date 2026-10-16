"""State-backed virtual filesystem implementation."""

from __future__ import annotations

import base64
import errno
import io
import json
import os
from collections.abc import Iterable, Iterator, MutableMapping
from datetime import datetime, timezone
from typing import Any

from ._globbing import glob_from_list, segments_match
from .base import FileInfo, FileMetadata
from .context import suspend
from .virtualfile import LazyBinaryFile, VirtualFile, open_file


class _HostState(MutableMapping):
    """A VirtualFS's state, reached with filesystem interception suspended.

    The state lives on the host. One that keeps its values in real files
    reads, writes, deletes and lists them with Python's own ``open`` and
    ``os`` functions, which under ``patch()`` would be routed into the
    active filesystem instead of the host. Each call into the state runs
    with interception suspended for that call alone, in the calling
    context only, so the caller's own code and other threads stay patched.

    Keys are listed in full before they are handed back, since a lazy
    iterator would reach the state again after the suspension ends. Any
    other method the state offers (``get_many``, say) is suspended the
    same way.
    """

    __slots__ = ("_state",)

    def __init__(self, state: MutableMapping[str, Any]):
        self._state = state

    def __getitem__(self, key: str) -> Any:
        with suspend():
            return self._state[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with suspend():
            self._state[key] = value

    def __delitem__(self, key: str) -> None:
        with suspend():
            del self._state[key]

    def __contains__(self, key: object) -> bool:
        with suspend():
            return key in self._state

    def __iter__(self) -> Iterator[str]:
        with suspend():
            keys = list(self._state)
        return iter(keys)

    def __len__(self) -> int:
        with suspend():
            return len(self._state)

    def keys(self) -> list[str]:  # type: ignore[override]
        with suspend():
            return list(self._state.keys())

    def get(self, key: str, default: Any = None) -> Any:
        with suspend():
            return self._state.get(key, default)

    def pop(self, key: str, *default: Any) -> Any:
        with suspend():
            return self._state.pop(key, *default)

    def __getattr__(self, name: str) -> Any:
        # Reached for "_state" itself only before it is set, as when copy
        # or pickle builds an instance without calling __init__.
        if name == "_state":
            raise AttributeError(name)
        with suspend():
            attr = getattr(self._state, name)
        if not callable(attr):
            return attr

        def suspended(*args: Any, **kwargs: Any) -> Any:
            with suspend():
                return attr(*args, **kwargs)

        return suspended


class VirtualFS:
    """State-backed virtual filesystem with metadata tracking.

    Provides file operations backed by agent state. Each file is stored
    as a separate state key, enabling granular versioning with a versioned
    state such as a kvgit worktree.

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
                A state may also offer ``get_many(*keys)``, returning a
                mapping of the keys that exist to their values (as kvgit
                worktrees do). Scans that read many keys then ask for them
                in one call, which over a networked store is one round
                trip instead of one per key.
            max_size_mb: Maximum total size of all files in megabytes.
                None means unlimited.
        """
        self._state = _HostState(state if state is not None else {})
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
        """Paths matching a glob pattern, by path segment.

        The semantics are ``pathlib``'s, as ``IsolatedFS`` answers on
        Python 3.13 and later: ``*``, ``?`` and ``[...]`` match within
        one segment, ``**`` matches zero or more directories (a trailing
        one matches everything below, the directory itself included),
        hidden names match like any other, and directories match as well
        as files. Only the pattern's fixed leading directory is listed.

        An absolute pattern answers in absolute paths. A relative one
        searches from the working directory and answers relative to it,
        naming the working directory itself ``.``.
        """
        if pattern.startswith("/"):
            return glob_from_list(self, pattern)

        cwd = self.getcwd()
        base = cwd.rstrip("/")
        results = []
        for match in glob_from_list(self, f"{base}/{pattern}"):
            # Every match is the working directory or below it.
            results.append("." if match == cwd else match[len(base) + 1 :])
        # The matcher has no name for "/" itself; relative to it, it's ".".
        if base == "" and segments_match(
            [part for part in pattern.split("/") if part and part != "."], []
        ):
            results.append(".")
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
        keys = [key for key in list(self._state.keys()) if self.is_metadata_key(key)]
        found = self._read_many(keys)
        for key in keys:
            raw = found.get(key)
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

    def _read_many(self, keys: list[str]) -> dict[str, Any]:
        """The values of ``keys`` that exist in the state.

        One ``get_many`` call when the state offers it, else one ``get``
        per key.
        """
        if not keys:
            return {}
        get_many = getattr(self._state, "get_many", None)
        if callable(get_many):
            return dict(get_many(*keys))
        found: dict[str, Any] = {}
        for key in keys:
            value = self._state.get(key)
            if value is not None:
                found[key] = value
        return found

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

    @staticmethod
    def _absolute(normalized: str) -> str:
        """A normalized key as an absolute path, for handing back to a
        method that resolves its argument.

        ``resolve_path`` returns the key form, which has no leading
        slash; passed on as it is, the next method reads it as relative
        to the working directory and names a different path whenever
        that is not ``/``.
        """
        return "/" + normalized.strip("/")

    def _file_on_the_way(self, normalized: str) -> str | None:
        """The first file among the directories above a normalized path.

        A path is a file or a directory, never both, so nothing can be
        created below a file. Walks up from the parent and stops at the
        first directory: above a directory there is no file on the way.
        Returns the file's absolute path, or None.
        """
        parts = normalized.strip("/").split("/")
        for depth in range(len(parts) - 1, 0, -1):
            ancestor = "/" + "/".join(parts[:depth])
            if self.isdir(ancestor):
                return None
            if self.isfile(ancestor):
                return ancestor
        return None

    def _refuse_file_at(self, normalized: str) -> None:
        """Raise unless a file can be written at a normalized path.

        A directory there is ``IsADirectoryError``, and a file on the way
        to it ``NotADirectoryError``, as a real filesystem answers both.
        Either write would leave a path that is a file and a directory at
        once.
        """
        absolute = self._absolute(normalized)
        if self.isdir(absolute):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), absolute)
        if self._file_on_the_way(normalized) is not None:
            raise NotADirectoryError(
                errno.ENOTDIR, os.strerror(errno.ENOTDIR), absolute
            )

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
    ) -> VirtualFile | LazyBinaryFile | io.StringIO:
        """Open a file, returning a file-like object.

        Args:
            path: File path to open.
            mode: File mode ('r', 'rb', 'w', 'wb', 'a', 'ab', 'r+', 'rb+').
            **kwargs: Additional arguments (ignored for compatibility).

        Returns:
            File-like object for reading or writing. A binary read is a
            ``LazyBinaryFile``, which fetches the ranges it is asked for; a
            text read and every write mode buffer the whole file.

        Raises:
            FileNotFoundError: If reading a file that doesn't exist.
            ValueError: If mode is invalid.

        Note:
            This is the same ``open()`` the patch layer synthesizes for a
            backend that has none, built from this filesystem's own
            ``read()``, ``write()`` and ``stat()``. It is kept as a method
            because a caller holding a ``VirtualFS`` directly expects to be
            able to open a file on it, not because it does anything the
            protocol cannot.
        """
        return open_file(self, path, mode, **kwargs)

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        """Read file contents as bytes, optionally a byte range of them.

        The defaults read the whole file, so a caller that names neither
        argument gets exactly what it always got.

        Args:
            path: File path to read.
            offset: Byte position to start at, counted from the start of the
                file. A read at or past the end returns ``b""``.
            size: Number of bytes to return; negative reads to the end of the
                file. A range running past the end is truncated there rather
                than short-changing the caller with an error.

        Returns:
            The requested bytes.

        Raises:
            FileNotFoundError: If file doesn't exist.
            ValueError: If offset is negative. A file has no bytes before its
                start, and silently clamping to zero would hand back data the
                caller did not ask for.

        Note:
            The backing store is a ``MutableMapping[str, bytes]``, so the blob
            still crosses from the store whole and the range is a slice of it.
            The saving is upstream of that: a file object over this method
            fetches only the ranges it is asked for, and a backend that can
            range natively (an HTTP endpoint, a remote store) moves only those
            bytes.
        """
        if offset < 0:
            raise ValueError(f"negative read offset: {offset}")
        key = self._encode_path(path)
        content = self._state.get(key)
        if content is None:
            raise FileNotFoundError(path)
        if offset == 0 and size < 0:
            return content
        if size < 0:
            return content[offset:]
        return content[offset : offset + size]

    def read_many(self, paths: Iterable[str]) -> dict[str, bytes]:
        """Read several whole files in one call to the backing state.

        What ``read(path)`` returns for each path, gathered with a single
        ``get_many`` when the state offers one -- so a caller reading a
        whole tree pays one round trip to a remote store rather than one
        per file.

        Args:
            paths: File paths to read, resolved against the current
                working directory as ``read`` resolves them.

        Returns:
            Path (as given) to its bytes, for the paths that are files. A
            path that is not one -- missing, or a directory -- is left out
            rather than raised on: the caller asked about many at once and
            learns which exist from the answer.
        """
        wanted: dict[str, list[str]] = {}
        for path in paths:
            wanted.setdefault(self._encode_path(path), []).append(path)
        found = self._read_many(list(wanted))
        return {path: content for key, content in found.items() for path in wanted[key]}

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        """Write bytes to a file.

        Args:
            path: File path to write.
            content: Content to write (must be bytes).
            mode: Write mode ('w' for write/overwrite, 'a' for append).

        Raises:
            TypeError: If content is not bytes.
            IsADirectoryError: If path is a directory.
            NotADirectoryError: If a path on the way to it is a file.
            OSError: If write would exceed max_size_mb limit.
        """
        if not isinstance(content, bytes):
            raise TypeError(f"Expected bytes, got {type(content).__name__}")

        resolved = self.resolve_path(path)
        normalized = self._normalize_path(resolved)
        self._refuse_file_at(normalized)

        # Auto-create parent directories
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

        Every path is checked before any is written, so a batch that is
        refused writes nothing.

        Raises:
            TypeError: If any content is not bytes.
            IsADirectoryError: If any path is a directory.
            NotADirectoryError: If a path on the way to any of them is a
                file, already or because the batch writes one there.
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

        # The batch as a whole must leave each path a file or a directory:
        # none may be a directory already, none may sit below a file, and
        # none may sit below another path the batch writes as a file.
        batch = {self._canonical_path(path) for path in files}
        for normalized in batch:
            self._refuse_file_at(normalized)
            parts = normalized.split("/")
            for depth in range(len(parts) - 1, 0, -1):
                if "/".join(parts[:depth]) in batch:
                    raise NotADirectoryError(
                        errno.ENOTDIR,
                        os.strerror(errno.ENOTDIR),
                        self._absolute(normalized),
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
            FileExistsError: If path exists, as a file or -- unless exist_ok
                is True -- as a directory. Any path isdir() reports True for
                exists, including a directory that is there only because a
                file path passes through it.
            FileNotFoundError: If parent doesn't exist and parents=False.
            NotADirectoryError: If a path on the way to it is a file.
        """
        if parents:
            self.makedirs(path, exist_ok=exist_ok)
            return

        path = self.resolve_path(path)
        normalized = self._normalize_path(path)

        # Validate parent exists
        parent = "/".join(normalized.split("/")[:-1])
        if parent and not self.isdir("/" + parent):
            if self._file_on_the_way(normalized) is not None:
                absolute = self._absolute(normalized)
                raise NotADirectoryError(
                    errno.ENOTDIR, os.strerror(errno.ENOTDIR), absolute
                )
            raise FileNotFoundError(f"No such file or directory: '{path}'")

        # Check if already exists (on the absolute path: ``path`` is the
        # key form, and checked as it is it would resolve a second time)
        absolute = self._absolute(normalized)
        if self.isfile(absolute):
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), absolute)
        if self.isdir(absolute):
            if exist_ok:
                return
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), absolute)

        # Create the directory's own row (implicit directories have none)
        now = datetime.now(timezone.utc).isoformat()
        self._meta_put(
            normalized,
            FileMetadata(size=0, created_at=now, modified_at=now, is_dir=True),
        )
        self._dir_cache = None  # Invalidate cache

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Create directory tree.

        Creates all parent directories as needed. With ``exist_ok`` False the
        directory must not already be there: anything ``isdir()`` reports True
        for counts as there, whether it was created by ``mkdir()`` or exists
        only because a file path passes through it. A caller cannot tell the
        two apart through this interface, so neither does the check.

        Args:
            path: Directory path.
            exist_ok: If True, don't raise if directory exists.

        Raises:
            FileExistsError: If path exists as a file, or if it exists as a
                directory and exist_ok is False.
            NotADirectoryError: If a path on the way to it is a file.
        """
        path = self.resolve_path(path)

        if not exist_ok and self.isdir(self._absolute(path)):
            absolute = self._absolute(path)
            raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), absolute)

        parts = path.strip("/").split("/")

        # Create each parent directory
        for i in range(len(parts)):
            dir_path = "/" + "/".join(parts[: i + 1])
            if self.isfile(dir_path):
                if i < len(parts) - 1:
                    # A file on the way, as os.makedirs() reports it.
                    raise NotADirectoryError(
                        errno.ENOTDIR, os.strerror(errno.ENOTDIR), self._absolute(path)
                    )
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), dir_path)
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
        # Checked on the absolute path: ``path`` is the key form, and the
        # checks resolve their argument again.
        absolute = self._absolute(normalized)

        if not self.exists(absolute):
            raise FileNotFoundError(f"No such directory: {absolute}")
        if self.isfile(absolute):
            raise NotADirectoryError(f"Not a directory: {absolute}")
        if not self.isdir(absolute):
            raise FileNotFoundError(f"No such directory: {absolute}")

        # Check if directory is empty
        children = self.list(absolute)
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

        Follows rename(2), as ``os.rename`` does on ``IsolatedFS``: a file
        replaces a file, and a directory replaces an empty directory. A
        missing parent of ``dst`` is created, as ``write`` creates one.
        The answers are the same on every platform. Where the hosts
        differ, the one POSIX names is used: a file onto the directory
        that holds it is ``IsADirectoryError``, as on macOS, where Linux
        checks the directory for emptiness first and raises ENOTEMPTY.

        Raises:
            FileNotFoundError: If source doesn't exist.
            IsADirectoryError: If src is a file and dst a directory.
            NotADirectoryError: If src is a directory and dst a file, or a
                path on the way to dst is a file.
            OSError: ENOTEMPTY if src is a directory and dst a directory
                that is not empty; EINVAL if dst is inside src.
        """
        src_resolved = self.resolve_path(src)
        dst_resolved = self.resolve_path(dst)
        src_norm = self._normalize_path(src_resolved)
        dst_norm = self._normalize_path(dst_resolved)
        dst_abs = self._absolute(dst_norm)

        src_is_file = self.isfile(src)
        if not src_is_file and not self.isdir(src):
            raise FileNotFoundError(src)
        if src_norm == dst_norm:
            # Renaming a path onto itself changes nothing.
            return
        if self._file_on_the_way(dst_norm) is not None:
            raise NotADirectoryError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), dst_abs)

        if src_is_file:
            if self.isdir(dst_abs):
                raise IsADirectoryError(
                    errno.EISDIR, os.strerror(errno.EISDIR), dst_abs
                )
        else:
            if src_norm == "/" or dst_norm.startswith(src_norm.rstrip("/") + "/"):
                raise OSError(
                    errno.EINVAL,
                    "Cannot move a directory into itself",
                    dst_abs,
                )
            if self.isfile(dst_abs):
                raise NotADirectoryError(
                    errno.ENOTDIR, os.strerror(errno.ENOTDIR), dst_abs
                )
            if self.isdir(dst_abs):
                if self.list(dst_abs):
                    raise OSError(
                        errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY), dst_abs
                    )
                # An empty directory is replaced: its row goes, and src's
                # tree takes its place.
                self._meta_drop(dst_norm)
                self._dir_cache = None

        if src_is_file:
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

        else:
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
            List of FileInfo objects sorted by path relative to the queried
            directory, so a directory precedes its contents. ``name`` is the
            entry's own name, the last component of ``path``, at every depth;
            it is not what the list is ordered by.

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

            # Display path preserves the user's queried prefix; the name
            # is the last component alone, whatever depth the entry sits at.
            basename = name.rsplit("/", 1)[-1]
            display = f"{user_prefix}/{name}" if user_prefix != "." else name

            meta = self._meta_at(internal_path)

            # Check if it's a directory
            if self.isdir(absolute):
                now = self._now_iso()
                result.append(
                    FileInfo(
                        name=basename,
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
                        name=basename,
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
                        name=basename,
                        path=display,
                        size=len(content),
                        created_at=now,
                        modified_at=now,
                        is_dir=False,
                    )
                )

        return result
