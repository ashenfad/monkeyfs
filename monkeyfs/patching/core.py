"""Patching infrastructure: originals, safe paths, and helpers."""

import builtins
import os
import os.path
import site
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from ..base import FileMetadata

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
    # Low-level fd operations
    "os_open": os.open,
    "os_read": os.read,
    "os_write": os.write,
    "os_close": os.close,
    "os_fstat": os.fstat,
    "os_lseek": os.lseek,
}

# Store fcntl originals (Posix only)
try:
    import fcntl as _fcntl_mod

    _originals["fcntl"] = _fcntl_mod.fcntl
    _originals["flock"] = _fcntl_mod.flock
    _originals["lockf"] = _fcntl_mod.lockf
    _has_fcntl = True
except ImportError:
    _fcntl_mod = None  # type: ignore[assignment]
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
