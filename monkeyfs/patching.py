"""FileSystem patching infrastructure.

Provides context-aware patching of Python's filesystem operations (builtins.open,
os.listdir, etc.) to route through any FileSystem Protocol implementation when
active. Uses contextvars for async-safe isolation between concurrent tasks.

The patching is applied once at module import. Each patched function checks
the context variable to determine whether to use the active filesystem or
the real filesystem.
"""

import builtins
import errno
import io
import os
import os.path
import pathlib
import re
import shutil
import site
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator

from .base import FileMetadata
from .context import current_fs

# Store original implementations once at import
_originals: dict[str, Any] = {
    "open": builtins.open,
    "listdir": os.listdir,
    "remove": os.remove,
    "unlink": os.unlink,
    "mkdir": os.mkdir,
    "makedirs": os.makedirs,
    "rmdir": os.rmdir,
    "rename": os.rename,
    "stat": os.stat,
    "lstat": os.lstat,
    "exists": os.path.exists,
    "isfile": os.path.isfile,
    "isdir": os.path.isdir,
    "islink": os.path.islink,
    "lexists": os.path.lexists,
    "samefile": os.path.samefile,
    "realpath": os.path.realpath,
    "abspath": os.path.abspath,
    "getsize": os.path.getsize,
    "scandir": os.scandir,
    "getcwd": os.getcwd,
    "chdir": os.chdir,
    "utime": os.utime,
    "touch": Path.touch,
    "expanduser": os.path.expanduser,
    "getenv": os.getenv,
    "expandvars": os.path.expandvars,
    "replace": os.replace,
    "access": os.access,
    "readlink": os.readlink,
    "symlink": os.symlink,
    "link": os.link,
    "chmod": os.chmod,
    "truncate": os.truncate,
    **({"chown": os.chown} if hasattr(os, "chown") else {}),
}

# Store fcntl originals (Posix only)
try:
    import fcntl as _fcntl_mod

    _originals["fcntl"] = _fcntl_mod.fcntl
    _originals["flock"] = _fcntl_mod.flock
    _originals["lockf"] = _fcntl_mod.lockf
    _has_fcntl = True
except ImportError:
    _has_fcntl = False


# Define safe system paths for read-only passthrough
# We allow access to stdlib and site-packages even when FS is active
# to support libraries that load their own resources (e.g., plotly, transformers).
def _get_safe_paths() -> list[str]:
    paths = {
        sys.base_prefix,
        sys.prefix,
        sys.exec_prefix,
        sys.base_exec_prefix,
    }
    # Add site packages
    for p in site.getsitepackages():
        paths.add(p)
    if hasattr(site, "getusersitepackages"):
        paths.add(site.getusersitepackages())

    # Resolve all paths
    return [str(Path(p).resolve()) for p in paths if os.path.exists(p)]


_SAFE_SYSTEM_PATHS = _get_safe_paths()


# Recursion guard for safe path checks
_in_safe_path_check: ContextVar[bool] = ContextVar("in_safe_path_check", default=False)

# Recursion guard for FS operations - prevents state backend internal file ops
# from being intercepted
_in_vfs_operation: ContextVar[bool] = ContextVar("in_vfs_operation", default=False)


def _is_safe_system_path(path: str | Path) -> bool:
    """Check if path is within a safe system directory."""
    try:
        # Prevent recursion when realpath calls lstat/stat
        token = _in_safe_path_check.set(True)
        try:
            # Resolve to absolute path using os.path.realpath
            path_str = os.path.realpath(path)
        finally:
            _in_safe_path_check.reset(token)

        return any(path_str.startswith(sp) for sp in _SAFE_SYSTEM_PATHS)
    except (OSError, ValueError):
        return False


def _require(fs: Any, method: str) -> Any:
    """Get a method from the fs, or raise NotImplementedError if missing."""
    fn = getattr(fs, method, None)
    if fn is None:
        raise NotImplementedError(f"{type(fs).__name__} does not implement {method}()")
    return fn


def _fs_list(fs: Any, path: str) -> list[str]:
    """List directory children via fs.list()."""
    return fs.list(path)


def _metadata_to_stat_result(meta: FileMetadata) -> os.stat_result:
    """Convert FileMetadata to os.stat_result."""
    return os.stat_result(
        (
            meta.st_mode,
            meta.st_ino,
            meta.st_dev,
            meta.st_nlink,
            meta.st_uid,
            meta.st_gid,
            meta.st_size,
            meta.st_atime,
            meta.st_mtime,
            meta.st_ctime,
        )
    )


# FS-aware wrapper functions


def _vfs_open(path: Any, *args: Any, **kwargs: Any) -> Any:
    """FileSystem-aware open() replacement."""
    mode = args[0] if args else kwargs.get("mode", "r")

    # Recursion guard: if we're already in an FS operation, use original open
    if _in_vfs_operation.get():
        return _originals["open"](path, *args, **kwargs)

    fs = current_fs.get()
    if fs is not None and isinstance(path, (str, Path)):
        token = _in_vfs_operation.set(True)
        try:
            return fs.open(str(path), mode, **kwargs)
        except (PermissionError, FileNotFoundError):
            if (
                "w" not in mode
                and "a" not in mode
                and "+" not in mode
                and "x" not in mode
                and _is_safe_system_path(path)
            ):
                return _originals["open"](path, *args, **kwargs)
            raise
        finally:
            _in_vfs_operation.reset(token)

    return _originals["open"](path, *args, **kwargs)


def _vfs_listdir(path: str = ".") -> list[str]:
    """FileSystem-aware os.listdir() replacement."""
    fs = current_fs.get()
    if fs is not None:
        path_str = str(path)
        try:
            if fs.isdir(path_str):
                return _fs_list(fs, path_str)
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            if _is_safe_system_path(path):
                return _originals["listdir"](path)
            raise

        if _is_safe_system_path(path) and _originals["isdir"](path):
            return _originals["listdir"](path)

        raise FileNotFoundError(
            errno.ENOENT, f"No such file or directory: '{path}'", path
        )

    return _originals["listdir"](path)


class MockDirEntry:
    """Mock os.DirEntry for FS items."""

    def __init__(
        self,
        name: str,
        is_dir: bool,
        stat_result: os.stat_result | None = None,
        path: str | None = None,
    ):
        self.name = name
        self.path = path if path is not None else name
        self._is_dir = is_dir
        self._stat = stat_result

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return self._is_dir

    def is_file(self, follow_symlinks: bool = True) -> bool:
        return not self._is_dir

    def is_symlink(self) -> bool:
        return False

    def stat(self, follow_symlinks: bool = True) -> os.stat_result:
        if self._stat is None:
            raise FileNotFoundError(f"No stat available for {self.name}")
        return self._stat

    def inode(self) -> int:
        return 0

    def __str__(self) -> str:
        return self.path

    def __fspath__(self) -> str:
        return self.path

    def __bytes__(self) -> bytes:
        return os.fsencode(self.path)

    def __repr__(self) -> str:
        return f"<MockDirEntry '{self.name}'>"


class ScandirWrapper:
    """Wrapper to make generator compatible with os.scandir context manager protocol."""

    def __init__(self, iterator: Iterator[os.DirEntry[str]]):
        self._iterator = iterator

    def __iter__(self) -> "ScandirWrapper":
        return self

    def __next__(self) -> os.DirEntry[str]:
        return next(self._iterator)

    def __enter__(self) -> "ScandirWrapper":
        return self

    def __exit__(self, *args: Any) -> None:
        pass


def _vfs_scandir(path: str = ".") -> Any:
    """FileSystem-aware os.scandir() replacement."""
    if current_fs.get() is None:
        return _originals["scandir"](path)

    def _scan_gen() -> Iterator[os.DirEntry[str]]:
        fs = current_fs.get()
        if fs is not None:
            path_str = str(path)
            try:
                if not fs.isdir(path_str):
                    raise NotADirectoryError(f"Not a directory: {path}")

                names = _fs_list(fs, path_str)
                for name in names:
                    child_path = os.path.join(path_str, name)
                    try:
                        meta = fs.stat(child_path)
                        st = _metadata_to_stat_result(meta)
                        is_d = fs.isdir(child_path)
                        yield MockDirEntry(name, is_d, st, path=child_path)  # type: ignore[misc]
                    except (FileNotFoundError, OSError):
                        continue
                return

            except (PermissionError, FileNotFoundError, NotADirectoryError):
                if _is_safe_system_path(path):
                    with _originals["scandir"](path) as it:
                        yield from it
                    return
                raise

        with _originals["scandir"](path) as it:
            yield from it

    return ScandirWrapper(_scan_gen())


def _vfs_remove(path: str, **kwargs: Any) -> None:
    """FileSystem-aware os.remove() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return fs.remove(str(path))
    return _originals["remove"](path, **kwargs)


def _vfs_unlink(path: str, **kwargs: Any) -> None:
    """FileSystem-aware os.unlink() replacement (alias for remove)."""
    fs = current_fs.get()
    if fs is not None:
        return fs.remove(str(path))
    return _originals["unlink"](path, **kwargs)


def _vfs_mkdir(path: str, mode: int = 0o777, **kwargs: Any) -> None:
    """FileSystem-aware os.mkdir() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return fs.mkdir(str(path))
    return _originals["mkdir"](path, mode, **kwargs)


def _vfs_makedirs(path: str, mode: int = 0o777, exist_ok: bool = False) -> None:
    """FileSystem-aware os.makedirs() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return fs.makedirs(str(path), exist_ok=exist_ok)
    return _originals["makedirs"](path, mode, exist_ok=exist_ok)


def _vfs_rmdir(path: str, *, dir_fd: int | None = None) -> None:
    """FileSystem-aware os.rmdir() replacement."""
    if dir_fd is not None:
        return _originals["rmdir"](path, dir_fd=dir_fd)

    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "rmdir")(str(path))
    return _originals["rmdir"](path)


def _vfs_rename(src: str, dst: str, **kwargs: Any) -> None:
    """FileSystem-aware os.rename() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return fs.rename(str(src), str(dst))
    return _originals["rename"](src, dst, **kwargs)


def _vfs_stat(path: str, **kwargs: Any) -> Any:
    """FileSystem-aware os.stat() replacement."""
    if _in_safe_path_check.get():
        return _originals["stat"](path, **kwargs)

    fs = current_fs.get()
    if fs is not None:
        path_str = str(path)
        try:
            meta = fs.stat(path_str)
            return _metadata_to_stat_result(meta)
        except PermissionError:
            if _is_safe_system_path(path):
                return _originals["stat"](path, **kwargs)
            raise
        except (FileNotFoundError, NotADirectoryError):
            if _is_safe_system_path(path):
                return _originals["stat"](path, **kwargs)
            # Re-raise with errno so pathlib and other consumers work correctly
            raise FileNotFoundError(
                errno.ENOENT, os.strerror(errno.ENOENT), path_str
            ) from None

    return _originals["stat"](path, **kwargs)


def _vfs_lstat(path: str, **kwargs: Any) -> Any:
    """FileSystem-aware os.lstat() replacement."""
    if _in_safe_path_check.get():
        return _originals["lstat"](path, **kwargs)

    fs = current_fs.get()
    if fs is not None:
        # FS implementations don't distinguish lstat from stat;
        # delegate to _vfs_stat which handles errno for FileNotFoundError
        return _vfs_stat(path, **kwargs)

    return _originals["lstat"](path, **kwargs)


def _vfs_exists(path: str, **kwargs: Any) -> bool:
    """FileSystem-aware os.path.exists() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            if fs.exists(str(path)):
                return True
        except PermissionError:
            pass
        if _is_safe_system_path(path):
            return _originals["exists"](path, **kwargs)
        return False

    return _originals["exists"](path, **kwargs)


def _vfs_isfile(path: str, **kwargs: Any) -> bool:
    """FileSystem-aware os.path.isfile() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            if fs.isfile(str(path)):
                return True
        except PermissionError:
            pass
        if _is_safe_system_path(path):
            return _originals["isfile"](path, **kwargs)
        return False

    return _originals["isfile"](path, **kwargs)


def _vfs_isdir(path: str, **kwargs: Any) -> bool:
    """FileSystem-aware os.path.isdir() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            if fs.isdir(str(path)):
                return True
        except PermissionError:
            pass
        if _is_safe_system_path(path):
            return _originals["isdir"](path, **kwargs)
        return False

    return _originals["isdir"](path, **kwargs)


def _vfs_islink(path: str, **kwargs: Any) -> bool:
    """FileSystem-aware os.path.islink() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            return _require(fs, "islink")(str(path))
        except PermissionError:
            pass
        if _is_safe_system_path(path):
            return _originals["islink"](path, **kwargs)
        return False

    return _originals["islink"](path, **kwargs)


def _vfs_lexists(path: str, **kwargs: Any) -> bool:
    """FileSystem-aware os.path.lexists() replacement."""
    return _vfs_exists(path, **kwargs)


def _vfs_samefile(path1: str, path2: str, **kwargs: Any) -> bool:
    """FileSystem-aware os.path.samefile() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            return _require(fs, "samefile")(str(path1), str(path2))
        except (PermissionError, FileNotFoundError):
            if _is_safe_system_path(path1) and _is_safe_system_path(path2):
                return _originals["samefile"](path1, path2, **kwargs)
            raise

    return _originals["samefile"](path1, path2, **kwargs)


def _vfs_realpath(path: str | os.PathLike[Any], **kwargs: Any) -> str:
    """FileSystem-aware os.path.realpath() replacement."""
    if _in_safe_path_check.get():
        return _originals["realpath"](path, **kwargs)

    fs = current_fs.get()
    if fs is not None:
        path_str = str(path)
        try:
            return _require(fs, "realpath")(path_str)
        except PermissionError:
            if _is_safe_system_path(path):
                return _originals["realpath"](path, **kwargs)
            return "/"

    return _originals["realpath"](path, **kwargs)


def _vfs_getsize(path: str, **kwargs: Any) -> int:
    """FileSystem-aware os.path.getsize() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            return _require(fs, "getsize")(str(path))
        except (PermissionError, FileNotFoundError):
            if _is_safe_system_path(path):
                return _originals["getsize"](path, **kwargs)
            raise

    return _originals["getsize"](path, **kwargs)


def _vfs_getcwd() -> str:
    """FileSystem-aware os.getcwd() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return fs.getcwd()
    return _originals["getcwd"]()


def _vfs_chdir(path: str) -> None:
    """FileSystem-aware os.chdir() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return fs.chdir(str(path))
    return _originals["chdir"](path)


def _vfs_abspath(path: str | os.PathLike[Any]) -> str:
    """FileSystem-aware os.path.abspath() replacement."""
    fs = current_fs.get()
    if fs is not None:
        path_str = str(path)
        if not os.path.isabs(path_str):
            path_str = os.path.join(fs.getcwd(), path_str)
        result = os.path.normpath(path_str)
        if not result.startswith("/"):
            result = "/" + result
        return result

    return _originals["abspath"](path)


def _vfs_expanduser(path: str | os.PathLike[Any]) -> str:
    """FileSystem-aware os.path.expanduser() replacement.

    When FS is active, expands '~' to '/' (the virtual root)
    instead of the real home directory to prevent path leaks.
    """
    if current_fs.get() is not None:
        path_str = os.fspath(path) if not isinstance(path, str) else path
        if path_str.startswith("~"):
            return "/" + path_str[2:] if path_str.startswith("~/") else "/"
        return path_str

    return _originals["expanduser"](path)


def _vfs_getenv(key: str, default: str | None = None) -> str | None:
    """FileSystem-aware os.getenv() replacement.

    When FS is active, returns '/' for 'HOME' requests
    to prevent home directory path leaks.
    """
    if current_fs.get() is not None and key == "HOME":
        return "/"

    return _originals["getenv"](key, default)


def _vfs_expandvars(path: str | os.PathLike[Any]) -> str:
    """FileSystem-aware os.path.expandvars() replacement.

    When FS is active, replaces $HOME with '/' to prevent
    home directory path leaks.
    """
    if current_fs.get() is not None:
        path_str = os.fspath(path) if not isinstance(path, str) else path
        path_str = re.sub(r"\$HOME/", "/", path_str)
        path_str = re.sub(r"\$\{HOME\}/", "/", path_str)
        path_str = re.sub(r"\$HOME\b", "/", path_str)
        path_str = re.sub(r"\$\{HOME\}", "/", path_str)
        return path_str

    return _originals["expandvars"](path)


def _vfs_utime(
    path: str | bytes | os.PathLike[Any],
    times: tuple[int, int] | tuple[float, float] | None = None,
    **kwargs: Any,
) -> None:
    """FileSystem-aware os.utime() replacement."""
    fs = current_fs.get()
    if fs is not None:
        path_str = str(path)
        if fs.exists(path_str):
            return
        if _is_safe_system_path(path_str):
            return _originals["utime"](path_str, times, **kwargs)
        raise FileNotFoundError(
            errno.ENOENT, f"No such file or directory: '{path_str}'", path_str
        )

    return _originals["utime"](path, times, **kwargs)


def _vfs_replace(src: str, dst: str, **kwargs: Any) -> None:
    """FileSystem-aware os.replace() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "replace")(str(src), str(dst))
    return _originals["replace"](src, dst, **kwargs)


def _vfs_access(path: str, mode: int, **kwargs: Any) -> bool:
    """FileSystem-aware os.access() replacement."""
    fs = current_fs.get()
    if fs is not None:
        try:
            return _require(fs, "access")(str(path), mode)
        except (PermissionError, FileNotFoundError):
            if _is_safe_system_path(path):
                return _originals["access"](path, mode, **kwargs)
            raise
    return _originals["access"](path, mode, **kwargs)


def _vfs_readlink(path: str, **kwargs: Any) -> str:
    """FileSystem-aware os.readlink() replacement."""
    if _in_safe_path_check.get():
        return _originals["readlink"](path, **kwargs)

    fs = current_fs.get()
    if fs is not None:
        try:
            return _require(fs, "readlink")(str(path))
        except (PermissionError, FileNotFoundError, OSError):
            if _is_safe_system_path(path):
                return _originals["readlink"](path, **kwargs)
            raise
    return _originals["readlink"](path, **kwargs)


def _vfs_symlink(src: str, dst: str, *args: Any, **kwargs: Any) -> None:
    """FileSystem-aware os.symlink() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "symlink")(str(src), str(dst))
    return _originals["symlink"](src, dst, *args, **kwargs)


def _vfs_link(src: str, dst: str, **kwargs: Any) -> None:
    """FileSystem-aware os.link() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "link")(str(src), str(dst))
    return _originals["link"](src, dst, **kwargs)


def _vfs_chmod(path: str, mode: int, **kwargs: Any) -> None:
    """FileSystem-aware os.chmod() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "chmod")(str(path), mode)
    return _originals["chmod"](path, mode, **kwargs)


def _vfs_chown(path: str, uid: int, gid: int, **kwargs: Any) -> None:
    """FileSystem-aware os.chown() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "chown")(str(path), uid, gid)
    return _originals["chown"](path, uid, gid, **kwargs)


def _vfs_truncate(path: str, length: int) -> None:
    """FileSystem-aware os.truncate() replacement."""
    fs = current_fs.get()
    if fs is not None:
        return _require(fs, "truncate")(str(path), length)
    return _originals["truncate"](path, length)


def _vfs_fcntl(fd: int, cmd: int, arg: Any = 0) -> Any:
    """FileSystem-aware fcntl.fcntl() replacement — no-op under VFS."""
    if current_fs.get() is not None:
        return 0
    return _originals["fcntl"](fd, cmd, arg)


def _vfs_flock(fd: int, operation: int) -> None:
    """FileSystem-aware fcntl.flock() replacement — no-op under VFS."""
    if current_fs.get() is not None:
        return
    return _originals["flock"](fd, operation)


def _vfs_lockf(
    fd: int, cmd: int, length: int = 0, start: int = 0, whence: int = 0
) -> Any:
    """FileSystem-aware fcntl.lockf() replacement — no-op under VFS."""
    if current_fs.get() is not None:
        return None
    return _originals["lockf"](fd, cmd, length, start, whence)


def _vfs_touch(self: Path, mode: int = 0o666, exist_ok: bool = True) -> None:
    """FileSystem-aware pathlib.Path.touch() replacement."""
    if exist_ok:
        try:
            if self.exists():
                os.utime(self, None)
                return
        except FileNotFoundError:
            pass

        with builtins.open(self, "a"):
            pass
    else:
        with builtins.open(self, "x"):
            pass


_lock = threading.Lock()
_installed = False


def install() -> None:
    """Install FS-aware patches to builtins and os module (idempotent, permanent).

    Safe to call multiple times — only the first call applies patches.
    Patches are inert when no filesystem is active (current_fs is None).
    """
    global _installed
    with _lock:
        if _installed:
            return
        _install()
        _installed = True


def _install() -> None:
    """Internal: apply all patches. Called once by install()."""
    # Patch builtins
    builtins.open = _vfs_open  # type: ignore[assignment]
    io.open = _vfs_open  # type: ignore[assignment]
    # Note: _io.open is NOT patched — it's the C-level implementation that
    # CPython uses internally for sys.stdout, TextIOWrapper, pagers, etc.
    # Patching it breaks internal I/O machinery.

    # Patch os module
    os.listdir = _vfs_listdir  # type: ignore[assignment]
    os.remove = _vfs_remove  # type: ignore[assignment]
    os.unlink = _vfs_unlink  # type: ignore[assignment]
    os.mkdir = _vfs_mkdir  # type: ignore[assignment]
    os.makedirs = _vfs_makedirs  # type: ignore[assignment]
    os.rmdir = _vfs_rmdir  # type: ignore[assignment]
    os.rename = _vfs_rename  # type: ignore[assignment]
    os.stat = _vfs_stat  # type: ignore[assignment]
    os.lstat = _vfs_lstat  # type: ignore[assignment]
    os.scandir = _vfs_scandir  # type: ignore[assignment]
    os.getcwd = _vfs_getcwd  # type: ignore[assignment]
    os.chdir = _vfs_chdir  # type: ignore[assignment]
    os.utime = _vfs_utime  # type: ignore[assignment]
    os.replace = _vfs_replace  # type: ignore[assignment]
    os.access = _vfs_access  # type: ignore[assignment]
    os.readlink = _vfs_readlink  # type: ignore[assignment]
    os.symlink = _vfs_symlink  # type: ignore[assignment]
    os.link = _vfs_link  # type: ignore[assignment]
    os.chmod = _vfs_chmod  # type: ignore[assignment]
    os.truncate = _vfs_truncate  # type: ignore[assignment]
    if hasattr(os, "chown"):
        os.chown = _vfs_chown  # type: ignore[assignment]

    # Patch pathlib.Path.touch
    Path.touch = _vfs_touch  # type: ignore[assignment]

    # Patch os.path
    os.path.exists = _vfs_exists  # type: ignore[assignment]
    os.path.isfile = _vfs_isfile  # type: ignore[assignment]
    os.path.isdir = _vfs_isdir  # type: ignore[assignment]
    os.path.islink = _vfs_islink  # type: ignore[assignment]
    os.path.lexists = _vfs_lexists  # type: ignore[assignment]
    os.path.samefile = _vfs_samefile  # type: ignore[assignment]
    os.path.realpath = _vfs_realpath  # type: ignore[assignment]
    os.path.abspath = _vfs_abspath  # type: ignore[assignment]
    os.path.getsize = _vfs_getsize  # type: ignore[assignment]
    os.path.expanduser = _vfs_expanduser  # type: ignore[assignment]
    os.path.expandvars = _vfs_expandvars  # type: ignore[assignment]

    # Patch os.getenv
    os.getenv = _vfs_getenv  # type: ignore[assignment]

    # Copy metadata from originals to wrappers so agent.fn(open) registers as 'open'
    _vfs_open.__name__ = "open"
    _vfs_open.__doc__ = _originals["open"].__doc__
    _vfs_listdir.__name__ = "listdir"
    _vfs_remove.__name__ = "remove"
    _vfs_unlink.__name__ = "unlink"
    _vfs_mkdir.__name__ = "mkdir"
    _vfs_makedirs.__name__ = "makedirs"
    _vfs_rename.__name__ = "rename"
    _vfs_stat.__name__ = "stat"
    _vfs_lstat.__name__ = "lstat"
    _vfs_scandir.__name__ = "scandir"
    _vfs_getcwd.__name__ = "getcwd"
    _vfs_chdir.__name__ = "chdir"
    _vfs_utime.__name__ = "utime"
    _vfs_exists.__name__ = "exists"
    _vfs_isfile.__name__ = "isfile"
    _vfs_isdir.__name__ = "isdir"
    _vfs_islink.__name__ = "islink"
    _vfs_lexists.__name__ = "lexists"
    _vfs_samefile.__name__ = "samefile"
    _vfs_realpath.__name__ = "realpath"
    _vfs_abspath.__name__ = "abspath"
    _vfs_getsize.__name__ = "getsize"
    _vfs_expanduser.__name__ = "expanduser"
    _vfs_getenv.__name__ = "getenv"
    _vfs_expandvars.__name__ = "expandvars"
    _vfs_replace.__name__ = "replace"
    _vfs_access.__name__ = "access"
    _vfs_readlink.__name__ = "readlink"
    _vfs_symlink.__name__ = "symlink"
    _vfs_link.__name__ = "link"
    _vfs_chmod.__name__ = "chmod"
    _vfs_chown.__name__ = "chown"
    _vfs_truncate.__name__ = "truncate"
    _vfs_fcntl.__name__ = "fcntl"
    _vfs_flock.__name__ = "flock"
    _vfs_lockf.__name__ = "lockf"

    # Patch pathlib internal accessor (Python < 3.11, e.g. 3.10)
    if hasattr(pathlib, "_NormalAccessor"):
        accessor = pathlib._NormalAccessor  # type: ignore
        if hasattr(accessor, "stat"):
            accessor.stat = staticmethod(_vfs_stat)
        if hasattr(accessor, "lstat"):
            accessor.lstat = staticmethod(_vfs_lstat)
        if hasattr(accessor, "scandir"):
            accessor.scandir = staticmethod(_vfs_scandir)
        if hasattr(accessor, "open"):
            accessor.open = staticmethod(_vfs_open)
        if hasattr(accessor, "unlink"):
            accessor.unlink = staticmethod(_vfs_unlink)
        if hasattr(accessor, "rmdir"):
            accessor.rmdir = staticmethod(_vfs_rmdir)
        if hasattr(accessor, "rename"):
            accessor.rename = staticmethod(_vfs_rename)
        if hasattr(accessor, "mkdir"):
            accessor.mkdir = staticmethod(_vfs_mkdir)
        if hasattr(accessor, "listdir"):
            accessor.listdir = staticmethod(_vfs_listdir)
        if hasattr(accessor, "getcwd"):
            accessor.getcwd = staticmethod(_vfs_getcwd)

    # Patch pathlib.Path._globber (Python 3.13+)
    if hasattr(pathlib.Path, "_globber"):
        globber = pathlib.Path._globber  # type: ignore
        if hasattr(globber, "scandir"):
            globber.scandir = staticmethod(_vfs_scandir)
        if hasattr(globber, "lstat"):
            globber.lstat = staticmethod(_vfs_lstat)

    # Patch glob._StringGlobber (Python 3.13+) — caches os.lstat/os.scandir
    # at import time, so we need to point them at our wrappers.
    import glob as _glob_mod

    if hasattr(_glob_mod, "_StringGlobber"):
        sg = _glob_mod._StringGlobber  # type: ignore
        if hasattr(sg, "scandir"):
            sg.scandir = staticmethod(_vfs_scandir)
        if hasattr(sg, "lstat"):
            sg.lstat = staticmethod(_vfs_lstat)

    # Patch fcntl (Posix only) — no-op stubs prevent file locking errors on VFS
    if _has_fcntl:
        _fcntl_mod.fcntl = _vfs_fcntl  # type: ignore[assignment]
        _fcntl_mod.flock = _vfs_flock  # type: ignore[assignment]
        _fcntl_mod.lockf = _vfs_lockf  # type: ignore[assignment]


@contextmanager
def patch(fs: Any) -> Iterator[None]:
    """Patch filesystem calls to route through the given filesystem.

    Calls install() automatically on first use. It is async-safe —
    concurrent async tasks each get their own context.

    Args:
        fs: Any FileSystem Protocol implementation.

    Yields:
        None. File operations within the block will use the given filesystem.

    Example:
        >>> with patch(vfs):
        ...     with open("data.csv", "w") as f:
        ...         f.write("a,b,c")
    """
    install()

    # Disable shutil platform optimizations that bypass our patches.
    # _use_fd_functions: rmtree uses os.open/fstat/scandir(fd) — bypasses string-path patches.
    # _HAS_FCOPYFILE: macOS fcopyfile on raw fds — VFS files lack fileno(), wastes try/except.
    # _USE_CP_SENDFILE: Linux sendfile on raw fds — same issue.
    # _USE_CP_COPY_FILE_RANGE: Python 3.14+ copy_file_range — same issue.
    saved_shutil = {}
    for flag in (
        "_use_fd_functions",
        "_HAS_FCOPYFILE",
        "_USE_CP_SENDFILE",
        "_USE_CP_COPY_FILE_RANGE",
    ):
        if hasattr(shutil, flag):
            saved_shutil[flag] = getattr(shutil, flag)
            setattr(shutil, flag, False)

    token = current_fs.set(fs)
    try:
        yield
    finally:
        current_fs.reset(token)
        for flag, value in saved_shutil.items():
            setattr(shutil, flag, value)


def get_current_fs() -> Any | None:
    """Get the current filesystem for the async context.

    Returns:
        The current FileSystem, or None if not in an FS context.
    """
    return current_fs.get()


# Install patches at import time. They are inert when no filesystem
# is active (current_fs is None) and activate when patch() sets one.
install()
