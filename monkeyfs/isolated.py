"""Isolated filesystem with path restriction.

Provides real filesystem access restricted to a specific directory.
"""

from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import FileInfo, FileMetadata
from .context import suspend


class IsolatedFS:
    """FileSystem interface restricted to a root directory.

    All file operations are validated to ensure paths stay within the
    configured root directory.

    Security features:
    - Rejects paths outside root directory
    - Handles symlinks securely (validates resolved paths)
    - Normalizes all path variations (../, ./, etc.)
    """

    def __init__(self, root: str):
        """Initialize isolated filesystem.

        Args:
            root: Absolute path to root directory (created if missing).

        Raises:
            ValueError: If root is not an absolute path or is not a directory.
        """
        # Suspend interception during init to ensure real verify works even if VFS active
        with suspend():
            root_path = Path(root)
            if not root_path.is_absolute():
                raise ValueError(f"Root must be absolute path: {root}")

            self.root = root_path.resolve()
            if not self.root.exists():
                self.root.mkdir(parents=True, exist_ok=True)
            if not self.root.is_dir():
                raise ValueError(f"Root must be a directory: {root}")

        self._cwd = "/"

    # -------------------------------------------------------------------------
    # Working Directory
    # -------------------------------------------------------------------------

    def getcwd(self) -> str:
        """Get current working directory.

        Returns:
            Current working directory path (defaults to "/").
        """
        return self._cwd

    def chdir(self, path: str) -> None:
        """Change current working directory.

        Args:
            path: Directory path to change to.

        Raises:
            FileNotFoundError: If directory doesn't exist.
        """
        resolved_virtual = self.resolve_path(path)
        real_path = self._validate_path(resolved_virtual)
        with suspend():
            if not real_path.is_dir():
                raise FileNotFoundError(f"No such directory: '{path}'")
        self._cwd = "/" + resolved_virtual.lstrip("/") if resolved_virtual else "/"

    def glob(self, pattern: str) -> list[str]:
        """Return list of paths matching a glob pattern."""
        with suspend():
            # Handle absolute/relative patterns
            if pattern.startswith("/"):
                # Absolute pattern: treat as relative to root
                # e.g. /src/*.py -> root/src/*.py
                search_path = self.root
                search_pattern = pattern.lstrip("/")
            else:
                # Relative pattern: relative to CWD
                cwd = self.getcwd()  # e.g. /src
                # e.g. root/src
                search_path = self._validate_path(cwd.lstrip("/"))
                search_pattern = pattern

            try:
                # Use list to consume generator
                matches = list(search_path.glob(search_pattern))

                # Convert matches back to virtual paths
                results = []
                for m in matches:
                    # m is absolute real path e.g. /tmp/root/src/foo.py
                    # relative to root: src/foo.py
                    rel = m.relative_to(self.root)
                    virtual = "/" + str(rel)

                    if not pattern.startswith("/"):
                        # Convert absolute virtual to relative to CWD
                        # e.g. /src/foo.py, cwd=/src -> foo.py
                        cwd = self.getcwd()
                        if virtual.startswith(cwd):
                            if cwd == "/":
                                results.append(virtual.lstrip("/"))
                            else:
                                results.append(virtual[len(cwd) + 1 :])
                        else:
                            # Fallback
                            results.append(virtual)
                    else:
                        results.append(virtual)

                return sorted(results)
            except (OSError, ValueError):
                return []

    def resolve_path(self, path: str) -> str:
        """Resolve path (relative or absolute) against current working directory.

        Args:
            path: File or directory path (relative or absolute).

        Returns:
            Normalized absolute path (virtual, not host).
        """
        if path.startswith("/"):
            return os.path.normpath(path)
        cwd = self.getcwd()
        return os.path.normpath(f"{cwd}/{path}")

    def _validate_path(self, path: str | Path) -> Path:
        """Validate and resolve path to ensure it's within root.

        Args:
            path: File path to validate (absolute or relative to CWD).

        Returns:
            Resolved absolute path within root.

        Raises:
            PermissionError: If path escapes root directory.
        """
        with suspend():
            path_str = str(path)

            # For relative paths, prepend the CWD (but don't normalize yet)
            # This preserves path traversal attempts for security validation
            if not path_str.startswith("/"):
                cwd = self.getcwd()
                path_str = f"{cwd}/{path_str}"

            p = Path(path_str)

            # Treat virtual absolute paths as relative to the isolated root (chroot-like)
            # e.g. /foo -> root/foo, / -> root
            if p.is_absolute():
                # If path is already inside root (e.g. from resolve()), use it as is
                try:
                    p.relative_to(self.root)
                    resolved = p.resolve()
                except ValueError:
                    # Virtual absolute path - treat as relative to root
                    p = p.relative_to(p.anchor)
                    resolved = (self.root / p).resolve()
            else:
                # Resolve relative to root (handles .., symlinks, etc.)
                resolved = (self.root / p).resolve()

            # Final boundary check
            try:
                resolved.relative_to(self.root)
            except ValueError:
                raise PermissionError(
                    f"Path outside root: {resolved} (root: {self.root})"
                )

            return resolved

    def _validate_path_no_follow(self, path: str | Path) -> Path:
        """Validate path without following the final symlink component.

        Resolves the parent directory (following symlinks) to validate it's
        within root, but preserves the final path component unresolved.
        This is needed for symlink operations (readlink, islink, lexists).

        Args:
            path: File path to validate.

        Returns:
            Host path with resolved parent but unresolved final component.

        Raises:
            PermissionError: If path escapes root directory.
        """
        with suspend():
            path_str = str(path)

            # Resolve relative paths against CWD
            if not path_str.startswith("/"):
                cwd = self.getcwd()
                path_str = f"{cwd}/{path_str}"

            p = Path(path_str)

            # Map virtual absolute path to host path
            if p.is_absolute():
                try:
                    p.relative_to(self.root)
                    host_path = p
                except ValueError:
                    rel = p.relative_to(p.anchor)
                    host_path = self.root / rel
            else:
                host_path = self.root / p

            # Resolve only the parent, keep the final component unresolved
            parent_resolved = host_path.parent.resolve()
            try:
                parent_resolved.relative_to(self.root)
            except ValueError:
                raise PermissionError(
                    f"Path outside root: {parent_resolved} (root: {self.root})"
                )

            return parent_resolved / host_path.name

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """Open a file within the isolated filesystem.

        Args:
            path: File path to open (relative to root).
            mode: File mode ('r', 'w', 'rb', 'wb', etc.).
            **kwargs: Additional arguments passed to open().

        Returns:
            File object.

        Raises:
            PermissionError: If path is outside root.
            FileNotFoundError: If file doesn't exist (read mode).
        """
        with suspend():
            resolved = self._validate_path(path)
            return io.open(resolved, mode, **kwargs)

    def read(self, path: str) -> bytes:
        """Read entire file as bytes."""
        with suspend():
            resolved = self._validate_path(path)
            return resolved.read_bytes()

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        """Write bytes to file, creating parent directories if needed.

        Args:
            path: File path to write.
            content: Bytes to write.
            mode: Write mode ('w' for write, 'a' for append).
        """
        with suspend():
            resolved = self._validate_path(path)
            resolved.parent.mkdir(parents=True, exist_ok=True)

            if mode == "a":
                with resolved.open("ab") as f:
                    f.write(content)
            else:
                resolved.write_bytes(content)

    def exists(self, path: str) -> bool:
        """Check if path exists."""
        with suspend():
            resolved = self._validate_path(path)
            return resolved.exists()

    def isfile(self, path: str) -> bool:
        """Check if path is a file."""
        with suspend():
            resolved = self._validate_path(path)
            return resolved.is_file()

    def isdir(self, path: str) -> bool:
        """Check if path is a directory."""
        with suspend():
            resolved = self._validate_path(path)
            return resolved.is_dir()

    def islink(self, path: str) -> bool:
        """Check if path is a symbolic link."""
        with suspend():
            try:
                unresolved = self._validate_path_no_follow(path)
                return unresolved.is_symlink()
            except (PermissionError, FileNotFoundError):
                return False

    def lexists(self, path: str) -> bool:
        """Check if path exists (without following symlinks)."""
        with suspend():
            try:
                unresolved = self._validate_path_no_follow(path)
                return unresolved.exists() or unresolved.is_symlink()
            except (PermissionError, FileNotFoundError):
                return False

    def samefile(self, path1: str, path2: str) -> bool:
        """Check if two paths refer to the same file."""
        with suspend():
            r1 = self._validate_path(path1)
            r2 = self._validate_path(path2)
            return r1.resolve() == r2.resolve()

    def realpath(self, path: str) -> str:
        """Return the canonical path."""
        with suspend():
            resolved = self._validate_path(path)
            # Return path relative to the root, as if root was /
            return "/" + str(resolved.relative_to(self.root)).lstrip("/")

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        """List directory contents.

        Args:
            path: Directory path to list.
            recursive: If True, list all nested files and directories.
        """
        with suspend():
            resolved = self._validate_path(path)
            if not resolved.exists():
                raise FileNotFoundError(f"No such directory: '{path}'")
            if not resolved.is_dir():
                raise NotADirectoryError(f"Not a directory: '{path}'")

            if recursive:
                results = []
                for p in resolved.rglob("*"):
                    results.append(str(p.relative_to(resolved)))
                return sorted(results)
            else:
                return sorted([p.name for p in resolved.iterdir()])

    def remove(self, path: str) -> None:
        """Remove a file."""
        with suspend():
            resolved = self._validate_path(path)
            if resolved.is_dir():
                raise IsADirectoryError(f"Is a directory: {path}")
            resolved.unlink()

    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None:
        """Create a directory."""
        with suspend():
            resolved = self._validate_path(path)
            resolved.mkdir(parents=parents, exist_ok=exist_ok)

    def rmdir(self, path: str) -> None:
        """Remove an empty directory."""
        with suspend():
            resolved = self._validate_path(path)
            resolved.rmdir()

    def rename(self, src: str, dst: str) -> None:
        """Rename/move a file or directory."""
        with suspend():
            src_resolved = self._validate_path(src)
            dst_resolved = self._validate_path(dst)

            src_resolved.rename(dst_resolved)

    def replace(self, src: str, dst: str) -> None:
        """Replace dst with src."""
        self.rename(src, dst)

    def chmod(self, path: str, mode: int) -> None:
        """Change file mode bits."""
        with suspend():
            resolved = self._validate_path(path)
            resolved.chmod(mode)

    def chown(self, path: str, uid: int, gid: int) -> None:
        """Change file owner and group."""
        with suspend():
            resolved = self._validate_path(path)
            os.chown(resolved, uid, gid)

    def access(self, path: str, mode: int) -> bool:
        """Check file access permissions."""
        with suspend():
            resolved = self._validate_path(path)
            return os.access(resolved, mode)

    def readlink(self, path: str) -> str:
        """Read a symbolic link target."""
        with suspend():
            unresolved = self._validate_path_no_follow(path)
            target = os.readlink(unresolved)
            # Validate target stays within root
            try:
                target_path = Path(target)
                if target_path.is_absolute():
                    target_path.relative_to(self.root)
            except ValueError:
                raise PermissionError(f"Symlink target outside root: {target}")
            return target

    def symlink(self, src: str, dst: str) -> None:
        """Create a symbolic link."""
        with suspend():
            dst_resolved = self._validate_path(dst)
            # src is the target — validate it stays within root
            src_resolved = self._validate_path(src)
            dst_resolved.symlink_to(src_resolved)

    def link(self, src: str, dst: str) -> None:
        """Create a hard link."""
        with suspend():
            src_resolved = self._validate_path(src)
            dst_resolved = self._validate_path(dst)
            dst_resolved.hardlink_to(src_resolved)

    def truncate(self, path: str, length: int) -> None:
        """Truncate file to given length."""
        with suspend():
            resolved = self._validate_path(path)
            os.truncate(resolved, length)

    def stat(self, path: str) -> FileMetadata:
        """Get file metadata."""
        with suspend():
            resolved = self._validate_path(path)
            if not resolved.exists():
                raise FileNotFoundError(f"No such file: {path}")

            stat_result = resolved.stat()
            return FileMetadata(
                size=stat_result.st_size if not resolved.is_dir() else 0,
                created_at=datetime.fromtimestamp(
                    stat_result.st_ctime, tz=timezone.utc
                ).isoformat(),
                modified_at=datetime.fromtimestamp(
                    stat_result.st_mtime, tz=timezone.utc
                ).isoformat(),
                is_dir=resolved.is_dir(),
            )

    def get_metadata_snapshot(self) -> dict[str, FileMetadata]:
        """Get metadata for all files and directories by walking the root."""
        with suspend():
            result = {}
            for item in self.root.rglob("*"):
                rel_path = str(item.relative_to(self.root))
                st = item.stat()
                is_dir = item.is_dir()
                result[rel_path] = FileMetadata(
                    size=st.st_size if not is_dir else 0,
                    created_at=datetime.fromtimestamp(
                        st.st_ctime, tz=timezone.utc
                    ).isoformat(),
                    modified_at=datetime.fromtimestamp(
                        st.st_mtime, tz=timezone.utc
                    ).isoformat(),
                    is_dir=is_dir,
                )
            return result

    def getsize(self, path: str) -> int:
        """Get file size in bytes."""
        return self.stat(path).size

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        """Create directory tree (alias for mkdir with parents=True)."""
        self.mkdir(path, parents=True, exist_ok=exist_ok)

    def write_many(self, files: dict[str, bytes]) -> None:
        """Write multiple files at once."""
        for path, content in files.items():
            self.write(path, content)

    def remove_many(self, paths: list[str]) -> None:
        """Remove multiple files at once."""
        for path in paths:
            self.remove(path)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[FileInfo]:
        """List directory with detailed file information.

        Args:
            path: Directory path to list.
            recursive: If True, list all nested items.
        """
        with suspend():
            resolved = self._validate_path(path)

            if not resolved.is_dir():
                raise NotADirectoryError(f"Not a directory: {path}")

            result = []
            items = resolved.rglob("*") if recursive else resolved.iterdir()

            for item in items:
                rel_path = str(item.relative_to(self.root))
                stat_info = item.stat()

                result.append(
                    FileInfo(
                        name=item.name,
                        path=rel_path,
                        is_dir=item.is_dir(),
                        size=stat_info.st_size if item.is_file() else 0,
                        created_at=datetime.fromtimestamp(
                            stat_info.st_ctime, tz=timezone.utc
                        ).isoformat(),
                        modified_at=datetime.fromtimestamp(
                            stat_info.st_mtime, tz=timezone.utc
                        ).isoformat(),
                    )
                )

        return sorted(result, key=lambda x: x.path)
