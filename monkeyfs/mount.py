"""Mount filesystem — routes operations to backing filesystems by path prefix.

Composes multiple filesystems into a unified namespace. A base filesystem
handles all paths by default; additional filesystems can be mounted at
specific prefixes (e.g., "/chapters", "/data/external").
"""

from __future__ import annotations

import errno
import fnmatch
import os
import posixpath
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from .base import FileInfo, FileMetadata
from .virtualfile import open_file


def _read_many(fs: Any, paths: list[str]) -> dict[str, bytes]:
    """``fs.read_many(paths)``, or one ``read`` per path on a filesystem
    that predates it -- a mount can be any backend, and ``read_many`` is
    not one of the methods a backend has to have."""
    batch = getattr(fs, "read_many", None)
    if batch is not None:
        return batch(paths)
    found: dict[str, bytes] = {}
    for path in paths:
        try:
            found[path] = fs.read(path)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
            continue
    return found


def _optional(fs: Any, name: str, fallback: Callable[..., Any], *args: Any) -> Any:
    """``fs.<name>(*args)``, or ``fallback(fs, *args)`` on a filesystem
    without it.

    A mount can be any backend, and the optional reads are not methods a
    backend has to have. ``MountFS`` defines all of them, so the patch
    layer always finds one here and never gets to say "not implemented"
    itself; this is where that is decided instead. A backend that declares
    the method but raises ``NotImplementedError`` from it -- the protocol's
    own way of saying it lacks one -- counts as lacking it.

    ``getattr`` sees through ``ReadOnlyFS``, whose ``__getattr__`` passes
    the lookup on to the filesystem it wraps.
    """
    method = getattr(fs, name, None)
    if method is not None:
        try:
            return method(*args)
        except NotImplementedError:
            pass
    return fallback(fs, *args)


# The fallbacks below derive their answer from the required reads where it
# can be derived, and raise NotImplementedError where it cannot. They share
# one premise with the patch layer's ``_fs_islink``: a backend with no
# ``islink`` (or one that raises NotImplementedError) has no links, so a
# path names exactly one file and is never a link. A backend that has ``islink`` may have links, and gets no answer
# that depends on there being none.


def _not_implemented(fs: Any, name: str) -> NotImplementedError:
    return NotImplementedError(f"{type(fs).__name__} does not implement {name}()")


def _may_have_links(fs: Any, path: str) -> bool:
    """Whether ``fs`` has an ``islink``, found by asking it about ``path``.

    Asked rather than looked up, so that a stub raising
    ``NotImplementedError`` counts as missing here too, as in ``_optional``.
    """
    islink = getattr(fs, "islink", None)
    if islink is None:
        return False
    try:
        islink(path)
    except NotImplementedError:
        return False
    except OSError:
        pass
    return True


def _size_from_stat(fs: Any, path: str) -> int:
    return fs.stat(path).size


def _no_links(fs: Any, path: str) -> bool:
    return False


def _samefile_by_path(fs: Any, path1: str, path2: str) -> bool:
    # As VirtualFS answers: a missing path is not the same file as anything.
    if not (fs.exists(path1) and fs.exists(path2)):
        return False
    if path1 == path2:
        return True
    if _may_have_links(fs, path1):
        raise _not_implemented(fs, "samefile")
    return False


def _readlink_without_links(fs: Any, path: str) -> str:
    if _optional(fs, "islink", _no_links, path):
        raise _not_implemented(fs, "readlink")
    if not fs.exists(path):
        raise FileNotFoundError(errno.ENOENT, "No such file or directory", path)
    raise OSError(errno.EINVAL, "Not a symbolic link", path)


def _access_from_exists(fs: Any, path: str, mode: int) -> bool:
    # Whatever exists on a backend with only the reads can be read. Whether
    # it can be written or executed is not something the reads can tell.
    if mode & (os.W_OK | os.X_OK):
        raise _not_implemented(fs, "access")
    return fs.exists(path)


_WILDCARDS = frozenset("*?[")


def _glob_from_list(fs: Any, pattern: str) -> list[str]:
    """Match an absolute ``pattern`` against what ``list()`` reports.

    The semantics are ``pathlib``'s, as ``IsolatedFS`` answers on Python
    3.13 and later, so a mount over a host directory and a mount over an
    object agree: ``*``, ``?`` and ``[...]`` match within one path segment,
    a ``**`` segment matches zero or more directories (a trailing one
    matches everything below, the directory itself included), hidden
    names match like any other, and directories match as well as files.

    Only the pattern's fixed leading directory -- everything before the
    first segment with a wildcard -- is listed, recursively, so
    ``/data/2024/*.csv`` walks ``/data/2024`` and nothing else.
    """
    parts = [part for part in pattern.split("/") if part]
    fixed: list[str] = []
    for part in parts:
        if _WILDCARDS.intersection(part):
            break
        fixed.append(part)
    root = "/" + "/".join(fixed)
    if len(fixed) == len(parts):
        return [root] if fs.exists(root) else []
    if not fs.isdir(root):
        return []

    rest = parts[len(fixed) :]
    base = root.rstrip("/")
    found = [
        f"{base}/{entry}"
        for entry in fs.list(root, recursive=True)
        if _segments_match(rest, entry.split("/"))
    ]
    # A pattern of only ``**`` below the fixed directory matches that
    # directory too, as pathlib's does -- unless it is the root, which
    # has no name to report.
    if root != "/" and _segments_match(rest, []):
        found.append(root)
    return sorted(found)


def _segments_match(pattern: list[str], parts: list[str]) -> bool:
    """Whether path segments ``parts`` match pattern segments ``pattern``."""
    if not pattern:
        return not parts
    head, tail = pattern[0], pattern[1:]
    if head == "**":
        while tail and tail[0] == "**":
            tail = tail[1:]
        return any(_segments_match(tail, parts[i:]) for i in range(len(parts) + 1))
    return (
        bool(parts)
        and fnmatch.fnmatchcase(parts[0], head)
        and _segments_match(tail, parts[1:])
    )


def _patterns_below(pattern: list[str], prefix: list[str]) -> set[tuple[str, ...]]:
    """What a mount at ``prefix`` is asked for, given a glob ``pattern``.

    Both are lists of path segments. The pattern's leading segments must
    match the prefix's, with the segment semantics ``_segments_match``
    uses: ``*``, ``?`` and ``[...]`` match within one segment, and ``**``
    matches zero or more. What is left of the pattern after the prefix is
    the mount's share, relative to its root: an empty one means the
    mount's root itself matches. A ``**`` still open when the prefix ends
    carries into the mount as ``**``, and its zero-length match is handed
    over explicitly as well, so a mount whose own ``glob`` needs a
    directory for a ``**`` (``VirtualFS``'s does) still answers for the
    files at its root. Adjacent ``**`` segments are one ``**``, as they
    are in pathlib, so the mount is not asked for one directory per
    ``**`` either. A pattern that cannot reach the prefix gives nothing.
    """
    collapsed: list[str] = []
    for part in pattern:
        if not (part == "**" and collapsed and collapsed[-1] == "**"):
            collapsed.append(part)
    pattern = collapsed
    found: set[tuple[str, ...]] = set()
    seen: set[tuple[int, int]] = set()
    stack = [(0, 0)]
    while stack:
        i, j = stack.pop()
        if (i, j) in seen:
            continue
        seen.add((i, j))
        if j == len(prefix):
            found.add(tuple(pattern[i:]))
            if i < len(pattern) and pattern[i] == "**":
                found.add(tuple(pattern[i + 1 :]))
            continue
        if i == len(pattern):
            continue
        head = pattern[i]
        if head == "**":
            stack.append((i + 1, j))  # matches nothing more of the prefix
            stack.append((i, j + 1))  # takes prefix[j] and stays open
        elif fnmatch.fnmatchcase(prefix[j], head):
            stack.append((i + 1, j + 1))
    return found


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

    def _owner_of(self, abs_path: str) -> str | None:
        """The mount prefix an absolute path routes to, or None for the base.

        The longest matching prefix wins, so a mount shadows the base
        beneath it and a nested mount shadows the mount it sits in. Every
        question of the form "which filesystem answers for this path?" --
        routing a call, translating a link target, deciding whether a
        path is reachable at all -- is this one function, so the answers
        cannot disagree.
        """
        for prefix in self._sorted_prefixes:
            if abs_path == prefix or abs_path.startswith(prefix + "/"):
                return prefix
        return None

    def _resolve(self, path: str) -> tuple[Any, str]:
        """Route path to the appropriate filesystem.

        Returns:
            (filesystem, translated_path) — the path with the mount
            prefix stripped, or "/" if the path is the mount root.
        """
        abs_path = self._to_absolute(path)

        prefix = self._owner_of(abs_path)
        if prefix is not None:
            inner = abs_path[len(prefix) :] or "/"
            return self._mounts[prefix], inner

        # Strip leading slash for base FS — VirtualFS uses paths like
        # "file.txt" not "/file.txt". But keep "/" as "/".
        base_path = abs_path.lstrip("/") or "/"
        return self._base, base_path

    def _is_implied_dir(self, abs_path: str) -> bool:
        """Whether abs_path is a directory on the way to a mount prefix.

        The composition shows every such directory, whether the
        filesystem that owns its path holds it or not -- ``isdir`` says
        yes to ``/workspace`` with only ``/workspace/data`` mounted. A
        mount prefix is not one (its filesystem's root answers for it),
        and neither is ``/``.
        """
        return (
            abs_path != "/"
            and abs_path not in self._mounts
            and self._is_mount_point(abs_path)
        )

    def _refuse_file_over_implied_dir(self, path: str) -> None:
        """Raise ``IsADirectoryError`` if ``path`` is an implied directory.

        The composition shows it as a directory, but the filesystem that
        owns its path may hold nothing there and would take a file. A
        path is a file or a directory, never both, so the write is
        refused here, as a directory held by the backend refuses it.
        """
        abs_path = self._to_absolute(path)
        if self._is_implied_dir(abs_path):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), abs_path)

    def _rename_is_a_no_op(self, src: str, dst: str) -> bool:
        """Check a rename against what only the composition knows.

        An implied directory renamed onto itself changes nothing (True):
        no filesystem holds it, so there is none to ask. Any other path
        onto itself goes to the filesystem that owns it. An implied
        directory as the destination is refused here, since the
        filesystem owning its path may not hold it and would not refuse,
        but only after the source, in rename(2)'s order: a missing source
        is ENOENT whatever the destination. An implied directory always
        holds the way to a mount, so a directory onto one is ENOTEMPTY
        and a file EISDIR.
        """
        abs_src = self._to_absolute(src)
        abs_dst = self._to_absolute(dst)
        if abs_src == abs_dst:
            return self._is_implied_dir(abs_src)
        if not self._is_implied_dir(abs_dst):
            return False
        if not self.exists(src):
            raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), src)
        if self.isdir(src):
            raise OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY), abs_dst)
        raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), abs_dst)

    @contextmanager
    def _implied_parents(self, *paths: str) -> Iterator[None]:
        """Hold the implied directories that creates at ``paths`` land in.

        A file, directory or link created in an implied directory lands
        in the filesystem that owns the directory's path, which may not
        hold it. That filesystem is given the directory first, as the
        namespace already promised it. Only an implied directory is
        made: a missing one that leads to no mount stays missing, and
        the create fails there as it would without the composition.

        If the create raises, the directories made for it are removed
        again, deepest first, so a failed call leaves the filesystem
        underneath as it found it -- on a versioned base, that is the
        difference between a failed call and a staged change. One that
        is no longer empty by then stays: something did land in it. Only
        a failure the call itself raises is seen; a handle that defers
        its write to ``close()`` has been handed back by then, over a
        path that exists.
        """
        missing: list[str] = []
        for path in paths:
            abs_path = self._to_absolute(path)
            if abs_path in self._mounts:
                continue
            probe = posixpath.dirname(abs_path)
            while self._is_implied_dir(probe) and probe not in missing:
                fs, inner = self._resolve(probe)
                if fs.isdir(inner):
                    break
                missing.append(probe)
                probe = posixpath.dirname(probe)
        made_from_deepest = sorted(missing, key=lambda p: p.count("/"), reverse=True)
        try:
            for abs_dir in made_from_deepest:
                fs, inner = self._resolve(abs_dir)
                fs.makedirs(inner, exist_ok=True)
            yield
        except BaseException:
            for abs_dir in made_from_deepest:
                fs, inner = self._resolve(abs_dir)
                try:
                    fs.rmdir(inner)
                except OSError:
                    pass
            raise

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

    def resolve_path(self, path: str) -> str:
        """Resolve ``path`` against this MountFS's own working directory.

        The answer is in the composed namespace, not any one backing
        filesystem's -- routing happens after resolution, so a path that
        crosses into a mount still names the same file.
        """
        return self._to_absolute(path)

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
        """Open ``path`` on the filesystem that owns it.

        That filesystem's own ``open()`` is preferred where it has one, so a
        mount over real files keeps handing out real file objects. Where it
        has none -- ``open()`` is optional on a backend -- a file object is
        synthesized over its ``read()``/``write()``, so mounting a
        bytes-level filesystem does not make it unopenable.
        """
        creates = any(c in mode for c in "wax")
        if creates or "+" in mode:
            if "x" in mode and self._is_implied_dir(self._to_absolute(path)):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), path)
            self._refuse_file_over_implied_dir(path)
        with self._implied_parents(*([path] if creates else [])):
            fs, inner = self._resolve(path)
            backend_open = getattr(fs, "open", None)
            if backend_open is not None:
                return backend_open(inner, mode, **kwargs)
            return open_file(fs, inner, mode, **kwargs)

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        """Read bytes from the filesystem that owns ``path``, range and all.

        The range is forwarded rather than applied here: a backend that can
        answer a range natively should be asked for one, and composing a
        filesystem must not turn its ranged reads back into whole-file ones.
        """
        fs, inner = self._resolve(path)
        return fs.read(inner, offset, size)

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
        """List what a read at each path would serve.

        The owning filesystem answers for the path itself; every mount
        point below it is injected, with the implicit parents that lead
        to it. A recursive listing walks every mount point under the
        path, however deeply it nests, and drops any entry a deeper
        mount shadows, so the listing and ``_owner_of`` agree about
        which filesystem stands behind each name.
        """
        abs_path = self._to_absolute(path)

        # If listing inside a mount, just delegate
        fs, inner = self._resolve(path)
        owner = self._owner_of(abs_path)

        # Check if listing a mount point root
        is_exact_mount = abs_path in self._mounts

        if is_exact_mount:
            result = set(fs.list("/", recursive=recursive))
        elif self._is_mount_point(abs_path):
            # An implicit parent of a mount is a directory whether or not
            # its owner (the base or an outer mount) holds one: what the
            # owner has under it is listed, and nothing there is fine.
            try:
                result = set(fs.list(inner, recursive=recursive))
            except (FileNotFoundError, NotADirectoryError):
                result = set()
        else:
            result = set(fs.list(inner, recursive=recursive))

        norm = abs_path.rstrip("/") if abs_path != "/" else ""
        if not recursive:
            result.update(self._mount_children(abs_path))
            return sorted(result)

        # An entry the owner lists under a deeper mount point is shadowed
        # by that mount: a read there never reaches the owner.
        result = {name for name in result if self._owner_of(f"{norm}/{name}") == owner}
        for prefix in self._mounts:
            if not prefix.startswith(norm + "/"):
                continue
            rel = prefix[len(norm) + 1 :]
            parts = rel.split("/")
            for depth in range(1, len(parts) + 1):
                result.add("/".join(parts[:depth]))
            for entry in self._mounts[prefix].list("/", recursive=True):
                if self._owner_of(f"{prefix}/{entry}") == prefix:
                    result.add(f"{rel}/{entry}")
        return sorted(result)

    def list_detailed(self, path: str = ".", recursive: bool = False) -> list[FileInfo]:
        names = self.list(path, recursive=recursive)
        abs_path = self._to_absolute(path)
        norm = abs_path.rstrip("/") if abs_path != "/" else ""
        user_prefix = path.rstrip("/")

        result = []
        for name in names:
            full_abs = f"{norm}/{name}" if norm else f"/{name}"
            display = f"{user_prefix}/{name}" if user_prefix != "." else name
            basename = name.rsplit("/", 1)[-1]
            meta = self.stat(full_abs)
            result.append(
                FileInfo(
                    name=basename,
                    path=display,
                    size=meta.size,
                    created_at=meta.created_at,
                    modified_at=meta.modified_at,
                    is_dir=meta.is_dir,
                )
            )
        return result

    def access(self, path: str, mode: int) -> bool:
        """Answer ``os.access()`` for where a create at ``path`` would land.

        An implied directory exists here whether or not the filesystem
        owning its path holds it, and what is created in it is made
        there (see ``_implied_parents``). Where that filesystem lacks
        it, the nearest directory up the path that is real answers:
        that is what a caller asking "may I write here" before creating
        a file has to be told.
        """
        probe = self._to_absolute(path)
        while self._is_implied_dir(probe):
            fs, inner = self._resolve(probe)
            if fs.isdir(inner):
                break
            probe = posixpath.dirname(probe)
        fs, inner = self._resolve(probe)
        return _optional(fs, "access", _access_from_exists, inner, mode)

    def getsize(self, path: str) -> int:
        fs, inner = self._resolve(path)
        return _optional(fs, "getsize", _size_from_stat, inner)

    def realpath(self, path: str) -> str:
        return self._to_absolute(path)

    def islink(self, path: str) -> bool:
        abs_path = self._to_absolute(path)
        if self._is_mount_point(abs_path):
            return False
        fs, inner = self._resolve(path)
        return _optional(fs, "islink", _no_links, inner)

    def lexists(self, path: str) -> bool:
        return self.exists(path)

    def samefile(self, path1: str, path2: str) -> bool:
        fs1, inner1 = self._resolve(path1)
        fs2, inner2 = self._resolve(path2)
        if fs1 is not fs2:
            return False
        return _optional(fs1, "samefile", _samefile_by_path, inner1, inner2)

    def readlink(self, path: str) -> str:
        """Read a symbolic link, translating the target into this namespace.

        A backend reports a link target in its own namespace, never in
        the host's: absolute means "from that filesystem's root", which
        is its mount point here, so the prefix goes back on. A relative
        target resolves against the link's own directory and names the
        same file on either side of the boundary, so it passes through.

        The prefix used is the one the *link* routed through, not the
        first mount that happens to hold this backend, so mounting one
        filesystem at two points still reads each link from where it was
        asked for.
        """
        abs_path = self._to_absolute(path)
        fs, inner = self._resolve(path)
        target = _optional(fs, "readlink", _readlink_without_links, inner)

        prefix = self._owner_of(abs_path)
        if prefix is None or not target.startswith("/"):
            return target
        return self._normalize(prefix + target)

    def get_metadata_snapshot(self) -> dict[str, FileMetadata]:
        """Metadata for every path reachable in the composed namespace.

        Keys are root-relative, as the backends report them, with each
        mount's paths carried under its prefix so two mounts holding the
        same inner path stay distinct.

        A path is reported by the filesystem an operation on it would
        route to, and by no other. A mount shadows the base beneath it,
        and a nested mount shadows the mount it sits in, so metadata a
        shadowed filesystem still holds for such a path is left out: it
        describes a file nothing here can open, stat or list, and a
        consumer comparing snapshots would read it as a live file.
        """
        snapshot: dict[str, FileMetadata] = {}
        sources: list[tuple[str | None, Any]] = [(None, self._base)]
        sources.extend(self._mounts.items())

        for prefix, fs in sources:
            base = "" if prefix is None else prefix.strip("/")
            for path, meta in fs.get_metadata_snapshot().items():
                inner = path.strip("/")
                composed = f"{base}/{inner}" if base and inner else (base or inner)
                if not composed or self._owner_of("/" + composed) != prefix:
                    continue
                snapshot[composed] = meta
        return snapshot

    def invalidate(self) -> None:
        """Drop lazy caches in every backing filesystem that keeps any.

        A backend without the method has no caches to drop, so it is
        skipped rather than made to fail the whole call.
        """
        for fs in (self._base, *self._mounts.values()):
            drop = getattr(fs, "invalidate", None)
            if drop is not None:
                drop()

    def read_many(self, paths: Iterable[str]) -> dict[str, bytes]:
        # Group by filesystem, so each one answers its share in one call
        groups: dict[int, tuple[Any, dict[str, list[str]]]] = {}
        for path in paths:
            if self._is_mount_point(self._to_absolute(path)):
                # A mount point, or a directory the composition makes on
                # the way to one: never a file here, whatever the base
                # holds under the same name, just as ``isfile`` says.
                continue
            fs, inner = self._resolve(path)
            key = id(fs)
            if key not in groups:
                groups[key] = (fs, {})
            groups[key][1].setdefault(inner, []).append(path)
        found: dict[str, bytes] = {}
        for fs, inner_paths in groups.values():
            for inner, content in _read_many(fs, list(inner_paths)).items():
                for path in inner_paths[inner]:
                    found[path] = content
        return found

    # -- Write operations --

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        self._refuse_file_over_implied_dir(path)
        with self._implied_parents(path):
            fs, inner = self._resolve(path)
            fs.write(inner, content, mode=mode)

    def write_many(self, files: dict[str, bytes]) -> None:
        for path in files:
            self._refuse_file_over_implied_dir(path)
        # Group by filesystem
        groups: dict[int, tuple[Any, dict[str, bytes]]] = {}
        for path, content in files.items():
            fs, inner = self._resolve(path)
            key = id(fs)
            if key not in groups:
                groups[key] = (fs, {})
            groups[key][1][inner] = content
        with self._implied_parents(*files):
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
        with self._implied_parents(path):
            fs, inner = self._resolve(path)
            fs.mkdir(inner, **kwargs)

    def makedirs(self, path: str, exist_ok: bool = True) -> None:
        fs, inner = self._resolve(path)
        fs.makedirs(inner, exist_ok=exist_ok)

    def rmdir(self, path: str) -> None:
        fs, inner = self._resolve(path)
        fs.rmdir(inner)

    def rename(self, src: str, dst: str) -> None:
        if self._rename_is_a_no_op(src, dst):
            return
        src_fs, src_inner = self._resolve(src)
        dst_fs, dst_inner = self._resolve(dst)
        with self._implied_parents(dst):
            if src_fs is dst_fs:
                src_fs.rename(src_inner, dst_inner)
            else:
                if src_fs.isdir(src_inner):
                    raise OSError(
                        errno.EXDEV, "Cross-mount directory rename not supported"
                    )
                content = src_fs.read(src_inner)
                dst_fs.write(dst_inner, content)
                src_fs.remove(src_inner)

    def replace(self, src: str, dst: str) -> None:
        if self._rename_is_a_no_op(src, dst):
            return
        src_fs, src_inner = self._resolve(src)
        dst_fs, dst_inner = self._resolve(dst)
        with self._implied_parents(dst):
            if src_fs is dst_fs:
                src_fs.replace(src_inner, dst_inner)
            else:
                content = src_fs.read(src_inner)
                dst_fs.write(dst_inner, content)
                src_fs.remove(src_inner)

    def symlink(self, src: str, dst: str) -> None:
        with self._implied_parents(dst):
            fs, inner = self._resolve(dst)
            fs.symlink(src, inner)

    def link(self, src: str, dst: str) -> None:
        src_fs, src_inner = self._resolve(src)
        dst_fs, dst_inner = self._resolve(dst)
        if src_fs is not dst_fs:
            raise OSError(errno.EXDEV, "Cross-mount link not supported")
        with self._implied_parents(dst):
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

    def utime(self, path: str, times: tuple[float, float] | None = None) -> None:
        fs, inner = self._resolve(path)
        fs.utime(inner, times)

    def glob(self, pattern: str) -> list[str]:
        """Absolute paths matching ``pattern``, across every mount it reaches.

        The base answers with its own ``glob``, less what a mount shadows.
        A mount answers when the pattern can reach it: its leading
        segments must match the mount's prefix, with ``*``, ``?`` and
        ``[...]`` matching one segment and ``**`` any number. So
        ``/workspace/**/test_*.py`` searches a mount at
        ``/workspace/docs``, and ``/workspace/*/test_*.py`` hands it
        ``/test_*.py``. The mount is asked with its own ``glob``, or
        answers from ``list()`` without one (see ``_glob_from_list``).

        The directories the composition shows without any filesystem
        holding them -- a mount's root, and every directory on the way to
        one -- match with those segment semantics too, as ``list()``
        shows them. Each backend matches its own content with its own
        ``glob``: ``IsolatedFS`` and the fallback by segments, and
        ``VirtualFS`` by ``fnmatch`` over the whole path, where ``*``
        crosses ``/`` and no directory matches.
        """
        abs_pattern = self._to_absolute(pattern)
        parts = [part for part in abs_pattern.split("/") if part]
        results = set()

        # Glob on base FS using absolute pattern (CWD-aware)
        for match in self._base.glob(abs_pattern):
            if not match.startswith("/"):
                match = "/" + match
            # Filter out paths that fall within mount prefixes —
            # mounts shadow the base FS at their prefix.
            if not any(
                match == p or match.startswith(p + "/") for p in self._sorted_prefixes
            ):
                results.add(match)

        # Glob on each mount the pattern reaches, with what is left of the
        # pattern below its prefix.
        for prefix, mount_fs in self._mounts.items():
            for rest in _patterns_below(parts, prefix.strip("/").split("/")):
                if not rest:
                    results.add(prefix)
                    continue
                inner_pattern = "/" + "/".join(rest)
                for match in _optional(
                    mount_fs, "glob", _glob_from_list, inner_pattern
                ):
                    if not match.startswith("/"):
                        match = "/" + match
                    # pathlib names the root of a trailing ** "."
                    full = prefix if match in ("/", "/.") else prefix + match
                    # A deeper mount shadows this one below its prefix.
                    if self._owner_of(full) == prefix:
                        results.add(full)

        # The directories on the way to a mount, which no filesystem may hold.
        for directory in self._implied_dirs():
            if _segments_match(parts, directory.strip("/").split("/")):
                results.add(directory)

        return sorted(results)

    def _implied_dirs(self) -> set[str]:
        """Every directory on the way to a mount prefix, other than ``/``
        and the prefixes themselves (see ``_is_implied_dir``)."""
        implied: set[str] = set()
        for prefix in self._mounts:
            parent = posixpath.dirname(prefix)
            while parent != "/":
                implied.add(parent)
                parent = posixpath.dirname(parent)
        return implied - set(self._mounts)
