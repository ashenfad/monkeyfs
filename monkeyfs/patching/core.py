"""Patching infrastructure: originals, safe paths, and helpers."""

import builtins
import errno
import os
import os.path
import site
import stat
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
    **({"link": os.link} if hasattr(os, "link") else {}),
    "chmod": os.chmod,
    **({"chflags": os.chflags} if hasattr(os, "chflags") else {}),
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

    # Resolve all paths (filter None for environments like Pyodide)
    return [
        str(Path(p).resolve()) for p in paths if p is not None and os.path.exists(p)
    ]


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

        path_str = os.path.normcase(path_str)
        for safe_path in _SAFE_SYSTEM_PATHS:
            safe_path = os.path.normcase(os.path.normpath(safe_path))
            try:
                if os.path.commonpath((path_str, safe_path)) == safe_path:
                    return True
            except ValueError:
                # Different drives on Windows cannot share a common path.
                continue
        return False
    except (OSError, ValueError):
        return False


_DIR_FD_KWARGS = ("dir_fd", "src_dir_fd", "dst_dir_fd")


def _reject_dir_fd(op: str, **kwargs: Any) -> None:
    """Refuse fd-relative path resolution while a filesystem is active.

    A ``dir_fd`` names a host kernel object, so the operation resolves against
    the real filesystem no matter what the active FileSystem says -- there is
    no path for monkeyfs to intercept, and a virtual filesystem has no fds the
    host would accept. Honoring it escapes the filesystem boundary; silently
    dropping it retargets the call at a different directory than the caller
    named, which quietly reintroduces the TOCTOU races dir_fd exists to avoid.

    Both are wrong, so fail loudly instead. ``ENOTSUP`` (rather than a
    permission error) is the accurate signal: this is an operation monkeyfs
    cannot emulate, and callers that probe ``os.supports_dir_fd`` and fall back
    to path-based resolution will do the right thing with it.
    """
    for name in _DIR_FD_KWARGS:
        if kwargs.get(name) is not None:
            raise OSError(
                errno.ENOTSUP,
                f"{name} is not supported by os.{op}() while a monkeyfs "
                "filesystem is active",
            )


def _require(fs: Any, method: str) -> Any:
    """Get a method from the fs, or raise NotImplementedError if missing."""
    fn = getattr(fs, method, None)
    if fn is None:
        raise NotImplementedError(f"{type(fs).__name__} does not implement {method}()")
    return fn


def _fs_list(fs: Any, path: str) -> list[str]:
    """List directory children via fs.list()."""
    return fs.list(path)


def _fs_islink(fs: Any, path: str) -> bool:
    """Ask the filesystem whether ``path`` is a symlink, leniently.

    ``islink()`` is not part of the ``FileSystem`` protocol -- it is one of the
    optional methods -- so a backend written to the documented interface may
    not have it. Callers here are answering "is this a link?" on paths they are
    about to describe rather than mutate, and a missing method means the
    backend has no links at all, so the honest answer is ``False``. Raising
    ``NotImplementedError`` (as ``os.path.islink()`` does, where the caller
    asked the question directly) would take out ``os.scandir()`` and
    ``os.lstat()`` for those backends instead.
    """
    fn = getattr(fs, "islink", None)
    if fn is None:
        return False
    try:
        return bool(fn(path))
    except OSError:
        # os.path.islink() answers False for anything it cannot look at; a
        # backend refusing the path (an escaping link, a missing parent) is
        # not evidence that a link is there.
        return False


def _symlink_stat_result(fs: Any, path: str, target: os.stat_result) -> os.stat_result:
    """Synthesize an ``lstat``-style ``stat_result`` for a symlink.

    No backend exposes ``lstat()``, so the link's own metadata cannot be read.
    What consumers actually branch on is the file type -- ``stat.S_ISLNK()`` --
    and the alternative is to keep reporting the *target's* type, which is the
    same lie that made ``rmtree`` walk through links. So the type bits are
    ``S_IFLNK`` and the rest is derived from the link target:

    * ``st_size`` is the length of the target string, which is what a real
      ``lstat()`` reports for a symlink;
    * timestamps, uid and gid are the target's, since the link has none we can
      reach. They are approximations, and the only fields here that are.

    Permission bits are ``0o777``, matching a real symlink on Linux (where they
    are meaningless and always fully open). ``copystat()`` would only apply
    them to the link itself, and neither ``chmod`` nor ``lchmod`` is reachable
    with ``follow_symlinks=False`` through the patch layer.

    Raises if the link target cannot be read (``OSError`` for a link out of
    the sandbox, ``NotImplementedError`` for a backend with no ``readlink()``).
    Callers fall back to their follow-the-link behavior, which keeps
    confinement errors intact for links pointing out of the sandbox.
    """
    link_target = _require(fs, "readlink")(path)
    return os.stat_result(
        (
            stat.S_IFLNK | 0o777,
            0,
            0,
            1,
            target.st_uid,
            target.st_gid,
            len(os.fsencode(link_target)),
            target.st_atime,
            target.st_mtime,
            target.st_ctime,
        )
    )


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
