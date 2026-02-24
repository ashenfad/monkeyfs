"""FS-aware wrapper functions for stdlib filesystem operations."""

import builtins
import errno
import os
import re
from pathlib import Path
from typing import Any, Iterator

from ..context import current_fs
from .core import (
    _fs_list,
    _in_safe_path_check,
    _in_vfs_operation,
    _is_safe_system_path,
    _metadata_to_stat_result,
    _originals,
    _require,
)
from .fdtable import _fd_table, _wrap_virtual_fd


def _vfs_open(path: Any, *args: Any, **kwargs: Any) -> Any:
    """FileSystem-aware open() replacement."""
    mode = args[0] if args else kwargs.get("mode", "r")

    # Recursion guard: if we're already in an FS operation, use original open
    if _in_vfs_operation.get():
        return _originals["open"](path, *args, **kwargs)

    fs = current_fs.get()

    # Integer fd path (os.fdopen): wrap virtual fd in a file object
    if isinstance(path, int) and _fd_table.is_virtual(path):
        wrap_kwargs = {
            k: kwargs[k] for k in ("encoding", "errors", "newline") if k in kwargs
        }
        return _wrap_virtual_fd(path, mode, _fd_table, **wrap_kwargs)

    if fs is not None and isinstance(path, (str, Path)):
        opener = kwargs.get("opener")

        # Opener path (NamedTemporaryFile): let opener call patched os.open
        if opener is not None:
            fd = opener(str(path), _mode_to_flags(mode))
            if _fd_table.is_virtual(fd):
                wrap_kwargs = {
                    k: kwargs[k]
                    for k in ("encoding", "errors", "newline")
                    if k in kwargs
                }
                return _wrap_virtual_fd(fd, mode, _fd_table, **wrap_kwargs)
            else:
                # Real fd from opener, use original fdopen
                return _originals["open"](fd, *args, **kwargs)

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


def _mode_to_flags(mode: str) -> int:
    """Convert Python open() mode string to os.open() flags."""
    flags = 0
    if "+" in mode:
        flags |= os.O_RDWR
    elif "w" in mode or "a" in mode or "x" in mode:
        flags |= os.O_WRONLY
    else:
        flags |= os.O_RDONLY

    if "w" in mode:
        flags |= os.O_CREAT | os.O_TRUNC
    elif "x" in mode:
        flags |= os.O_CREAT | os.O_EXCL
    elif "a" in mode:
        flags |= os.O_CREAT | os.O_APPEND

    return flags


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


# Low-level fd operation wrappers


def _vfs_os_open(
    path: Any, flags: int, mode: int = 0o777, *, dir_fd: Any = None
) -> int:
    """VFS-aware os.open() replacement."""
    if dir_fd is not None:
        return _originals["os_open"](path, flags, mode, dir_fd=dir_fd)

    if _in_vfs_operation.get():
        return _originals["os_open"](path, flags, mode)

    fs = current_fs.get()
    if fs is not None and isinstance(path, (str, Path)):
        path_str = str(path)

        # Read-only opens on safe system paths pass through
        is_write = flags & (
            os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND
        )
        if not is_write and _is_safe_system_path(path_str):
            return _originals["os_open"](path_str, flags, mode)

        token = _in_vfs_operation.set(True)
        try:
            return _fd_table.allocate(path_str, fs, flags, mode)
        except (PermissionError, FileNotFoundError):
            if not is_write and _is_safe_system_path(path_str):
                return _originals["os_open"](path_str, flags, mode)
            raise
        finally:
            _in_vfs_operation.reset(token)

    return _originals["os_open"](path, flags, mode)


def _vfs_os_read(fd: int, n: int) -> bytes:
    """VFS-aware os.read() replacement."""
    vfd = _fd_table.get(fd)
    if vfd is not None:
        if not vfd.readable:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return vfd.buffer.read(n) or b""
    return _originals["os_read"](fd, n)


def _vfs_os_write(fd: int, data: bytes) -> int:
    """VFS-aware os.write() replacement."""
    vfd = _fd_table.get(fd)
    if vfd is not None:
        if not vfd.writable:
            raise OSError(errno.EBADF, "Bad file descriptor")
        return vfd.buffer.write(data)
    return _originals["os_write"](fd, data)


def _vfs_os_close(fd: int) -> None:
    """VFS-aware os.close() replacement."""
    if _fd_table.is_virtual(fd):
        _fd_table.close(fd)
        return
    _originals["os_close"](fd)


def _vfs_os_fstat(fd: int) -> Any:
    """VFS-aware os.fstat() replacement."""
    vfd = _fd_table.get(fd)
    if vfd is not None:
        size = len(vfd.buffer.getvalue())
        try:
            meta = vfd.fs.stat(vfd.path)
            # Override size with current buffer size (may differ from persisted)
            from ..base import FileMetadata

            updated = FileMetadata(
                size=size,
                created_at=meta.created_at,
                modified_at=meta.modified_at,
                is_dir=meta.is_dir,
            )
            return _metadata_to_stat_result(updated)
        except FileNotFoundError:
            # File not yet persisted — synthesize minimal stat
            from datetime import datetime, timezone

            from ..base import FileMetadata

            now = datetime.now(timezone.utc).isoformat()
            return _metadata_to_stat_result(
                FileMetadata(size=size, created_at=now, modified_at=now)
            )
    return _originals["os_fstat"](fd)


def _vfs_os_lseek(fd: int, offset: int, whence: int) -> int:
    """VFS-aware os.lseek() replacement."""
    vfd = _fd_table.get(fd)
    if vfd is not None:
        return vfd.buffer.seek(offset, whence)
    return _originals["os_lseek"](fd, offset, whence)
