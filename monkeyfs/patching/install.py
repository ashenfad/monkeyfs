"""Patch installation and context manager."""

import builtins
import io
import os
import pathlib
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..context import current_fs
from .core import _fcntl_mod, _has_fcntl, _originals
from .patches import (
    _vfs_abspath,
    _vfs_access,
    _vfs_chdir,
    _vfs_chmod,
    _vfs_chown,
    _vfs_exists,
    _vfs_expanduser,
    _vfs_expandvars,
    _vfs_fcntl,
    _vfs_flock,
    _vfs_getcwd,
    _vfs_getenv,
    _vfs_getsize,
    _vfs_glob_scandir,
    _vfs_isdir,
    _vfs_isfile,
    _vfs_islink,
    _vfs_lexists,
    _vfs_link,
    _vfs_listdir,
    _vfs_lockf,
    _vfs_lstat,
    _vfs_makedirs,
    _vfs_mkdir,
    _vfs_open,
    _vfs_os_close,
    _vfs_os_fstat,
    _vfs_os_lseek,
    _vfs_os_open,
    _vfs_os_read,
    _vfs_os_write,
    _vfs_readlink,
    _vfs_realpath,
    _vfs_remove,
    _vfs_rename,
    _vfs_replace,
    _vfs_rmdir,
    _vfs_samefile,
    _vfs_scandir,
    _vfs_stat,
    _vfs_symlink,
    _vfs_touch,
    _vfs_truncate,
    _vfs_unlink,
    _vfs_utime,
)

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
        _apply_patches()
        _installed = True


def _apply_patches() -> None:
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

    # Patch low-level fd operations
    os.open = _vfs_os_open  # type: ignore[assignment]
    os.read = _vfs_os_read  # type: ignore[assignment]
    os.write = _vfs_os_write  # type: ignore[assignment]
    os.close = _vfs_os_close  # type: ignore[assignment]
    os.fstat = _vfs_os_fstat  # type: ignore[assignment]
    os.lseek = _vfs_os_lseek  # type: ignore[assignment]

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
    _vfs_os_open.__name__ = "open"
    _vfs_os_read.__name__ = "read"
    _vfs_os_write.__name__ = "write"
    _vfs_os_close.__name__ = "close"
    _vfs_os_fstat.__name__ = "fstat"
    _vfs_os_lseek.__name__ = "lseek"

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
    # Python 3.14 changed glob to expect (entry, name, path) 3-tuples from
    # scandir instead of raw DirEntry objects.
    import glob as _glob_mod

    if hasattr(_glob_mod, "_StringGlobber"):
        sg = _glob_mod._StringGlobber  # type: ignore
        if hasattr(sg, "scandir"):
            if sys.version_info >= (3, 14):
                sg.scandir = staticmethod(_vfs_glob_scandir)
            else:
                sg.scandir = staticmethod(_vfs_scandir)
        if hasattr(sg, "lstat"):
            sg.lstat = staticmethod(_vfs_lstat)

    # Patch fcntl (Posix only) — no-op stubs prevent file locking errors on VFS
    if _has_fcntl:
        _fcntl_mod.fcntl = _vfs_fcntl  # type: ignore[assignment]
        _fcntl_mod.flock = _vfs_flock  # type: ignore[assignment]
        _fcntl_mod.lockf = _vfs_lockf  # type: ignore[assignment]

    # Patch tempfile's cached os.unlink reference.
    # _TemporaryFileCloser.cleanup captures os.unlink as a default arg at
    # import time, bypassing our runtime patches. Re-bind it so
    # NamedTemporaryFile(delete=True) cleanup routes through the VFS.
    # The cleanup method with the cached default was added in Python 3.12.
    if hasattr(tempfile, "_TemporaryFileCloser") and hasattr(
        getattr(tempfile, "_TemporaryFileCloser"), "cleanup"
    ):
        _closer_cls = tempfile._TemporaryFileCloser  # type: ignore[attr-defined]
        _orig_cleanup = _closer_cls.cleanup
        defaults = list(_orig_cleanup.__defaults__ or [])
        # The 'unlink' default is the last positional default
        if defaults and callable(defaults[-1]):
            defaults[-1] = _vfs_unlink
            _orig_cleanup.__defaults__ = tuple(defaults)


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

    # Python 3.14+: _rmtree_impl is bound at import time, so setting
    # _use_fd_functions=False doesn't affect which rmtree runs. Override
    # _rmtree_impl directly to force the string-path-based implementation.
    if hasattr(shutil, "_rmtree_impl") and hasattr(shutil, "_rmtree_unsafe"):
        saved_shutil["_rmtree_impl"] = shutil._rmtree_impl  # type: ignore[attr-defined]
        shutil._rmtree_impl = shutil._rmtree_unsafe  # type: ignore[attr-defined]

    # Reset tempfile's cached tempdir so it re-evaluates inside VFS
    saved_tempdir = tempfile.tempdir
    tempfile.tempdir = None

    token = current_fs.set(fs)
    try:
        yield
    finally:
        current_fs.reset(token)
        tempfile.tempdir = saved_tempdir
        for flag, value in saved_shutil.items():
            setattr(shutil, flag, value)


def get_current_fs() -> Any | None:
    """Get the current filesystem for the async context.

    Returns:
        The current FileSystem, or None if not in an FS context.
    """
    return current_fs.get()
