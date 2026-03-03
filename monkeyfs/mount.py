"""Mount filesystem — routes operations to backing filesystems by path prefix.

Composes multiple filesystems into a unified namespace. A base filesystem
handles all paths by default; additional filesystems can be mounted at
specific prefixes (e.g., "/chapters", "/data/external").
"""

from __future__ import annotations

import errno
import os
from datetime import datetime, timezone
from typing import Any

from .base import FileInfo, FileMetadata


class MountFS:
    """Routes filesystem operations to backing filesystems by path prefix.

    Example:
        >>> from monkeyfs import VirtualFS, MountFS
        >>> from monkeyfs.readonly import ReadOnlyFS
        >>> base = VirtualFS({})
        >>> chapters = VirtualFS({})
        >>> chapters.write("summary.md", b"# Chapter 1")
        >>> fs = MountFS(base, {"/chapters": ReadOnlyFS(chapters)})
        >>> fs.read("/chapters/summary.md")
        b'# Chapter 1'
        >>> fs.write("/app/main.py", b"print('hi')")  # goes to base
    """

    def __init__(self, base: Any, mounts: dict[str, Any] | None = None):
        self._base = base
        self._mounts: dict[str, Any] = {}
        self._sorted_prefixes: list[str] = []
        self._cwd = "/"

        if mounts:
            for prefix, fs in mounts.items():
                self.mount(prefix, fs)

    def mount(self, prefix: str, fs: Any) -> None:
        """Mount a filesystem at the given prefix."""
        prefix = self._normalize(prefix)
        if prefix == "/":
            raise ValueError("Cannot mount at '/'; use the base filesystem")
        self._mounts[prefix] = fs
        self._sorted_prefixes = sorted(self._mounts.keys(), key=len, reverse=True)

    def unmount(self, prefix: str) -> None:
        """Unmount the filesystem at the given prefix."""
        prefix = self._normalize(prefix)
        if prefix not in self._mounts:
            raise ValueError(f"No mount at '{prefix}'")
        del self._mounts[prefix]
        self._sorted_prefixes = sorted(self._mounts.keys(), key=len, reverse=True)

    # -- Path resolution --

    @staticmethod
    def _normalize(path: str) -> str:
        """Normalize path to absolute form with no trailing slash."""
        if not path or path in (".", "./"):
            return "/"
        path = os.path.normpath(path).replace("\\", "/")
        if not path.startswith("/"):
            path = "/" + path
        return path if path == "/" else path.rstrip("/")

    def _to_absolute(self, path: str) -> str:
        """Resolve relative path against CWD, return normalized absolute."""
        if path.startswith("/"):
            return self._normalize(path)
        if self._cwd == "/":
            return self._normalize("/" + path)
        return self._normalize(self._cwd + "/" + path)

    def _resolve(self, path: str) -> tuple[Any, str]:
        """Route path to the appropriate filesystem.

        Returns:
            (filesystem, translated_path) — the path with the mount
            prefix stripped, or "/" if the path is the mount root.
        """
        abs_path = self._to_absolute(path)

        for prefix in self._sorted_prefixes:
            if abs_path == prefix or abs_path.startswith(prefix + "/"):
                inner = abs_path[len(prefix) :] or "/"
                return self._mounts[prefix], inner

        # Strip leading slash for base FS — VirtualFS uses paths like
        # "file.txt" not "/file.txt". But keep "/" as "/".
        base_path = abs_path.lstrip("/") or "/"
        return self._base, base_path

    def _is_mount_point(self, abs_path: str) -> bool:
        """Check if abs_path is a mount prefix or an implicit parent of one."""
        norm = abs_path.rstrip("/") if abs_path != "/" else ""
        for prefix in self._mounts:
            if prefix == abs_path or prefix.startswith(norm + "/"):
                return True
        return False

    def _mount_children(self, abs_path: str) -> list[str]:
        """Get immediate child names that are mount points under abs_path."""
        norm = abs_path.rstrip("/") if abs_path != "/" else ""
        children = set()
        for prefix in self._mounts:
            if norm == "":
                # Listing root — first component of each mount prefix
                child = prefix.lstrip("/").split("/")[0]
                children.add(child)
            elif prefix.startswith(norm + "/"):
                remainder = prefix[len(norm) + 1 :]
                child = remainder.split("/")[0]
                children.add(child)
        return sorted(children)

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    # -- CWD --

    def getcwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        abs_path = self._to_absolute(path)
        # Mount points are valid directories
        if self._is_mount_point(abs_path):
            self._cwd = abs_path
            return
        fs, inner = self._resolve(path)
        if not fs.isdir(inner):
            raise FileNotFoundError(f"No such directory: '{path}'")
        self._cwd = abs_path

    # -- Read operations --

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        fs, inner = self._resolve(path)
        return fs.open(inner, mode, **kwargs)

    def read(self, path: str) -> bytes:
        fs, inner = self._resolve(path)
        return fs.read(inner)

    def stat(self, path: str) -> FileMetadata:
        abs_path = self._to_absolute(path)
        if self._is_mount_point(abs_path):
            # Check if it's an exact mount prefix — delegate to the mount
            for prefix in self._sorted_prefixes:
                if abs_path == prefix:
                    return self._mounts[prefix].stat("/")
            # Implicit parent of a mount — synthesize directory metadata
            now = self._now_iso()
            return FileMetadata(size=0, created_at=now, modified_at=now, is_dir=True)
        fs, inner = self._resolve(path)
        return fs.stat(inner)

    def exists(self, path: str) -> bool:
        abs_path = self._to_absolute(path)
        if self._is_mount_point(abs_path):
            return True
        fs, inner = self._resolve(path)
        return fs.exists(inner)

    def isfile(self, path: str) -> bool:
        abs_path = self._to_absolute(path)
        if self._is_mount_point(abs_path):
            return False
        fs, inner = self._resolve(path)
        return fs.isfile(inner)

    def isdir(self, path: str) -> bool:
        abs_path = self._to_absolute(path)
        if self._is_mount_point(abs_path):
            return True
        fs, inner = self._resolve(path)
        return fs.isdir(inner)

    def list(self, path: str = ".", recursive: bool = False) -> list[str]:
        abs_path = self._to_absolute(path)

        # If listing inside a mount, just delegate
        fs, inner = self._resolve(path)

        # Check if listing a mount point root
        is_exact_mount = abs_path in self._mounts

        if is_exact_mount:
            result = set(fs.list("/", recursive=recursive))
        elif self._is_mount_point(abs_path) and fs is self._base:
            # Implicit parent of a mount but exists in base — try base
            try:
                result = set(fs.list(inner, recursive=recursive))
            except (FileNotFoundError, NotADirectoryError):
                result = set()
        else:
            result = set(fs.list(inner, recursive=recursive))

        # Inject mount-point children
        mount_children = self._mount_children(abs_path)
        for child in mount_children:
            result.add(child)
            if recursive:
                # Find the full prefix for this child
                norm = abs_path.rstrip("/") if abs_path != "/" else ""
                child_prefix = f"{norm}/{child}" if norm else f"/{child}"
                if child_prefix in self._mounts:
                    mount_fs = self._mounts[child_prefix]
                    for entry in mount_fs.list("/", recursive=True):
                        result.add(f"{child}/{entry}")

        return sorted(result)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[FileInfo]:
        names = self.list(path, recursive=recursive)
        abs_path = self._to_absolute(path)
        norm = abs_path.rstrip("/") if abs_path != "/" else ""

        result = []
        for name in names:
            full_abs = f"{norm}/{name}" if norm else f"/{name}"
            is_dir = self.isdir(full_abs)
            if is_dir:
                now = self._now_iso()
                result.append(
                    FileInfo(
                        name=name,
                        path=name,
                        size=0,
                        created_at=now,
                        modified_at=now,
                        is_dir=True,
                    )
                )
            else:
                meta = self.stat(full_abs)
                result.append(
                    FileInfo(
                        name=name,
                        path=name,
                        size=meta.size,
                        created_at=meta.created_at,
                        modified_at=meta.modified_at,
                        is_dir=False,
                    )
                )
        return result

    def access(self, path: str, mode: int) -> bool:
        fs, inner = self._resolve(path)
        return fs.access(inner, mode)

    def getsize(self, path: str) -> int:
        fs, inner = self._resolve(path)
        return fs.getsize(inner)

    def realpath(self, path: str) -> str:
        return self._to_absolute(path)

    def islink(self, path: str) -> bool:
        abs_path = self._to_absolute(path)
        if self._is_mount_point(abs_path):
            return False
        fs, inner = self._resolve(path)
        return fs.islink(inner)

    def lexists(self, path: str) -> bool:
        return self.exists(path)

    def samefile(self, path1: str, path2: str) -> bool:
        fs1, inner1 = self._resolve(path1)
        fs2, inner2 = self._resolve(path2)
        if fs1 is not fs2:
            return False
        return fs1.samefile(inner1, inner2)

    # -- Write operations --

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        fs, inner = self._resolve(path)
        fs.write(inner, content, mode=mode)

    def write_many(self, files: dict[str, bytes]) -> None:
        # Group by filesystem
        groups: dict[int, tuple[Any, dict[str, bytes]]] = {}
        for path, content in files.items():
            fs, inner = self._resolve(path)
            key = id(fs)
            if key not in groups:
                groups[key] = (fs, {})
            groups[key][1][inner] = content
        for fs, inner_files in groups.values():
            fs.write_many(inner_files)

    def remove(self, path: str) -> None:
        fs, inner = self._resolve(path)
        fs.remove(inner)

    def remove_many(self, paths: list[str]) -> None:
        # Group by filesystem
        groups: dict[int, tuple[Any, list[str]]] = {}
        for path in paths:
            fs, inner = self._resolve(path)
            key = id(fs)
            if key not in groups:
                groups[key] = (fs, [])
            groups[key][1].append(inner)
        for fs, inner_paths in groups.values():
            fs.remove_many(inner_paths)

    def mkdir(self, path: str, **kwargs: Any) -> None:
        fs, inner = self._resolve(path)
        fs.mkdir(inner, **kwargs)

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        fs, inner = self._resolve(path)
        fs.makedirs(inner, exist_ok=exist_ok)

    def rmdir(self, path: str) -> None:
        fs, inner = self._resolve(path)
        fs.rmdir(inner)

    def rename(self, src: str, dst: str) -> None:
        src_fs, src_inner = self._resolve(src)
        dst_fs, dst_inner = self._resolve(dst)
        if src_fs is dst_fs:
            src_fs.rename(src_inner, dst_inner)
        else:
            if src_fs.isdir(src_inner):
                raise OSError(errno.EXDEV, "Cross-mount directory rename not supported")
            content = src_fs.read(src_inner)
            dst_fs.write(dst_inner, content)
            src_fs.remove(src_inner)

    def replace(self, src: str, dst: str) -> None:
        src_fs, src_inner = self._resolve(src)
        dst_fs, dst_inner = self._resolve(dst)
        if src_fs is dst_fs:
            src_fs.replace(src_inner, dst_inner)
        else:
            content = src_fs.read(src_inner)
            dst_fs.write(dst_inner, content)
            src_fs.remove(src_inner)

    def symlink(self, src: str, dst: str) -> None:
        fs, inner = self._resolve(dst)
        fs.symlink(src, inner)

    def link(self, src: str, dst: str) -> None:
        src_fs, src_inner = self._resolve(src)
        dst_fs, dst_inner = self._resolve(dst)
        if src_fs is not dst_fs:
            raise OSError(errno.EXDEV, "Cross-mount link not supported")
        src_fs.link(src_inner, dst_inner)

    def chmod(self, path: str, mode: int) -> None:
        fs, inner = self._resolve(path)
        fs.chmod(inner, mode)

    def chown(self, path: str, uid: int, gid: int) -> None:
        fs, inner = self._resolve(path)
        fs.chown(inner, uid, gid)

    def truncate(self, path: str, length: int) -> None:
        fs, inner = self._resolve(path)
        fs.truncate(inner, length)

    def glob(self, pattern: str) -> list[str]:
        abs_pattern = self._to_absolute(pattern)
        results = set()

        # Glob on base FS
        for match in self._base.glob(pattern):
            # Ensure results are absolute
            if not match.startswith("/"):
                match = "/" + match
            results.add(match)

        # Glob on each mount
        for prefix, mount_fs in self._mounts.items():
            # Check if pattern could match under this prefix
            if abs_pattern.startswith(prefix + "/") or abs_pattern == prefix:
                inner_pattern = abs_pattern[len(prefix) :] or "/"
                for match in mount_fs.glob(inner_pattern):
                    if not match.startswith("/"):
                        match = "/" + match
                    results.add(prefix + match)

        return sorted(results)
