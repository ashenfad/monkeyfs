"""In-memory filesystem implementation."""

from __future__ import annotations

import errno as _errno
import fnmatch
import os
import posixpath
import stat as stat_mod
from io import BytesIO, StringIO
from typing import Any

class MemoryFS:
    """Simple in-memory filesystem.

    Stores files as ``bytes`` in a plain dict and tracks directories
    in a set.  Implements the full ``FileSystem`` protocol so it works
    transparently with ``use_fs()`` patching.

    Useful for testing and as a lightweight VFS for sandboxed agents
    that need to create and import local modules.
    """

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self._cwd = "/"

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        path = self._resolve(path)
        if "r" in mode:
            if path not in self.files:
                raise FileNotFoundError(_errno.ENOENT, "No such file", path)
            content = self.files[path]
            if "b" in mode:
                return BytesIO(content)
            return StringIO(content.decode("utf-8"))
        elif "w" in mode or "a" in mode:
            parent = posixpath.dirname(path)
            if parent != path and parent not in self.dirs:
                raise FileNotFoundError(_errno.ENOENT, "No such directory", parent)
            is_binary = "b" in mode
            buf = BytesIO() if is_binary else StringIO()
            if "a" in mode and path in self.files:
                existing = self.files[path]
                if is_binary:
                    buf.write(existing)
                else:
                    buf.write(existing.decode("utf-8"))
            original_close = buf.close

            def close_and_save() -> None:
                value = buf.getvalue()
                self.files[path] = value if is_binary else value.encode("utf-8")
                original_close()

            buf.close = close_and_save  # type: ignore[method-assign]
            return buf
        raise ValueError(f"Unsupported mode: {mode}")

    def read(self, path: str) -> bytes:
        path = self._resolve(path)
        if path not in self.files:
            raise FileNotFoundError(_errno.ENOENT, "No such file", path)
        return self.files[path]

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        path = self._resolve(path)
        self.files[path] = content
        # Ensure parent directories exist implicitly
        parent = posixpath.dirname(path)
        while parent and parent != "/":
            self.dirs.add(parent)
            parent = posixpath.dirname(parent)

    def stat(self, path: str) -> Any:
        path = self._resolve(path)
        if path in self.files:
            size = len(self.files[path])
            return os.stat_result((
                stat_mod.S_IFREG | 0o644,
                0, 0, 1, 1000, 1000,
                size,
                0, 0, 0,
            ))
        if path in self.dirs:
            return os.stat_result((
                stat_mod.S_IFDIR | 0o755,
                0, 0, 2, 1000, 1000,
                0, 0, 0, 0,
            ))
        raise FileNotFoundError(_errno.ENOENT, "No such file or directory", path)

    def list(self, path: str = ".") -> list[str]:
        """List immediate children of a directory."""
        path = self._resolve(path)
        if path not in self.dirs:
            raise FileNotFoundError(_errno.ENOENT, "No such directory", path)
        prefix = path.rstrip("/") + "/"
        entries: set[str] = set()
        for f in self.files:
            if f.startswith(prefix):
                rest = f[len(prefix):]
                entries.add(rest.split("/")[0])
        for d in self.dirs:
            if d.startswith(prefix) and d != path:
                rest = d[len(prefix):]
                if rest:
                    entries.add(rest.split("/")[0])
        return sorted(entries)

    def listdir(self, path: str = "/") -> list[str]:
        return self.list(path)

    def exists(self, path: str) -> bool:
        path = self._resolve(path)
        return path in self.files or path in self.dirs

    def isfile(self, path: str) -> bool:
        path = self._resolve(path)
        return path in self.files

    def isdir(self, path: str) -> bool:
        path = self._resolve(path)
        return path in self.dirs

    def islink(self, path: str) -> bool:
        return False

    def lexists(self, path: str) -> bool:
        return self.exists(path)

    def samefile(self, path1: str, path2: str) -> bool:
        return self._resolve(path1) == self._resolve(path2)

    def realpath(self, path: str) -> str:
        return self._resolve(path)

    def getsize(self, path: str) -> int:
        path = self._resolve(path)
        if path not in self.files:
            raise FileNotFoundError(_errno.ENOENT, "No such file", path)
        return len(self.files[path])

    def glob(self, pattern: str) -> list[str]:
        all_paths = list(self.files.keys())
        for d in self.dirs:
            if d != "/":
                all_paths.append(d)
        return sorted(fnmatch.filter(all_paths, pattern))

    def mkdir(self, path: str, *, parents: bool = False, exist_ok: bool = False) -> None:
        path = self._resolve(path)
        if parents:
            self.makedirs(path, exist_ok=exist_ok)
            return
        if path in self.dirs:
            if exist_ok:
                return
            raise FileExistsError(f"Directory exists: '{path}'")
        parent = posixpath.dirname(path)
        if parent != path and parent not in self.dirs:
            raise FileNotFoundError(_errno.ENOENT, "No such directory", parent)
        self.dirs.add(path)

    def makedirs(self, path: str, *, exist_ok: bool = False) -> None:
        path = self._resolve(path)
        parts = path.strip("/").split("/")
        current = ""
        for part in parts:
            current += "/" + part
            self.mkdir(current, exist_ok=True)

    def rmdir(self, path: str) -> None:
        path = self._resolve(path)
        if path not in self.dirs:
            raise FileNotFoundError(_errno.ENOENT, "No such directory", path)
        prefix = path.rstrip("/") + "/"
        for f in self.files:
            if f.startswith(prefix):
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", path)
        for d in self.dirs:
            if d.startswith(prefix) and d != path:
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", path)
        self.dirs.discard(path)

    def remove(self, path: str) -> None:
        path = self._resolve(path)
        if path not in self.files:
            raise FileNotFoundError(_errno.ENOENT, "No such file", path)
        del self.files[path]

    def rename(self, src: str, dst: str) -> None:
        src = self._resolve(src)
        dst = self._resolve(dst)
        if src in self.files:
            self.files[dst] = self.files.pop(src)
        elif src in self.dirs:
            src_prefix = src.rstrip("/") + "/"
            self.dirs.discard(src)
            self.dirs.add(dst)
            for d in list(self.dirs):
                if d.startswith(src_prefix):
                    self.dirs.discard(d)
                    self.dirs.add(dst + d[len(src):])
            for f in list(self.files):
                if f.startswith(src_prefix):
                    self.files[dst + f[len(src):]] = self.files.pop(f)
        else:
            raise FileNotFoundError(_errno.ENOENT, "No such file or directory", src)

    def getcwd(self) -> str:
        return self._cwd

    def chdir(self, path: str) -> None:
        path = self._resolve(path)
        if path not in self.dirs:
            raise FileNotFoundError(_errno.ENOENT, "No such directory", path)
        self._cwd = path

    def replace(self, src: str, dst: str) -> None:
        self.rename(src, dst)

    def access(self, path: str, mode: int) -> bool:
        return self.exists(path)

    def readlink(self, path: str) -> str:
        raise OSError(_errno.EINVAL, "Not a symbolic link", self._resolve(path))

    def symlink(self, src: str, dst: str) -> None:
        raise OSError(_errno.EPERM, "MemoryFS does not support symlinks")

    def link(self, src: str, dst: str) -> None:
        src = self._resolve(src)
        dst = self._resolve(dst)
        if src not in self.files:
            raise FileNotFoundError(_errno.ENOENT, "No such file", src)
        self.files[dst] = self.files[src]

    def chmod(self, path: str, mode: int) -> None:
        if not self.exists(path):
            raise FileNotFoundError(_errno.ENOENT, "No such file or directory", path)

    def chown(self, path: str, uid: int, gid: int) -> None:
        if not self.exists(path):
            raise FileNotFoundError(_errno.ENOENT, "No such file or directory", path)

    def truncate(self, path: str, length: int) -> None:
        path = self._resolve(path)
        if path not in self.files:
            raise FileNotFoundError(_errno.ENOENT, "No such file", path)
        self.files[path] = self.files[path][:length]

    def _resolve(self, path: str) -> str:
        """Resolve a path relative to cwd and normalize . and .. components."""
        if not path.startswith("/"):
            path = self._cwd.rstrip("/") + "/" + path
        return posixpath.normpath(path)
