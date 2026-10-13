"""Isolated filesystem with path restriction.

Provides real filesystem access restricted to a specific directory.
"""

from __future__ import annotations

import errno
import io
import os
from collections.abc import Iterable
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
        """Return list of paths matching a glob pattern.

        Matching is pathlib's, and the search stays inside the root (see
        ``_glob_inside``): a ``..`` above the root matches nothing, as
        every other method refuses it, and a link out of the root matches
        by its name but is never searched, as ``list()`` names it but
        won't list it.
        """
        with suspend():
            # Handle absolute/relative patterns
            if pattern.startswith("/"):
                # Absolute pattern: treat as relative to root
                # e.g. /src/*.py -> root/src/*.py
                search_path = self.root
                search_pattern = pattern.lstrip("/")
            else:
                # Relative pattern: relative to CWD. The cwd goes in absolute:
                # stripped of its slash, _validate_path would join it onto
                # the cwd a second time (#39).
                cwd = self.getcwd()  # e.g. /src
                # e.g. root/src
                search_path = self._validate_path(cwd)
                search_pattern = pattern

            try:
                matches = self._glob_inside(search_path, search_pattern)

                # Convert matches back to virtual paths. Each match is the
                # search root joined with what matched, e.g.
                # /tmp/root/src/foo.py.
                results = []
                for m in matches:
                    if pattern.startswith("/"):
                        # Relative to root: /src/foo.py
                        results.append("/" + str(m.relative_to(self.root)))
                    else:
                        # Relative to where the search started: foo.py, or
                        # "." for the cwd itself, which a ``**`` matches.
                        # The search root is the cwd resolved, so relating
                        # a match to the cwd as named would miss when the
                        # cwd goes through a link.
                        results.append(str(m.relative_to(search_path)))

                # Once each: a ``**`` before another wildcard and a ``**``
                # can reach the same path both ways (``**/*/**``).
                return sorted(set(results))
            except (OSError, ValueError):
                return []

    def _glob_inside(self, start: Path, pattern: str) -> list[Path]:
        """Match ``pattern`` from ``start``, a directory inside the root,
        without searching any directory outside it.

        pathlib searches every directory a segment leads to, following a
        ``..`` or a link wherever it goes, and the matches name what it
        found there. So no segment that leads to a directory to search is
        handed to it: those are walked here, keeping only directories that
        resolve inside the root, with ``..`` taken as the kernel takes it,
        from the directory before it. What is left of the pattern -- a run
        of ``**`` and one last segment -- is pathlib's to match from each
        directory kept. ``**`` doesn't follow links, so it stays below a
        directory inside, and the last segment only names entries of the
        directories it is matched in: a link out of the root is one, and
        matches by its name, as ``list()`` names it.

        A directory reached through a link out of the root and back in
        (``/up/<root>/a``) is refused with the link, as is ``..`` above
        the root even where a later name comes back in (``/../<root>``):
        such a match would spell out where the root is on the host.
        """
        parts = [part for part in pattern.split("/") if part not in ("", ".")]
        # A run of ``**`` matches what one does.
        parts = [
            part
            for i, part in enumerate(parts)
            if not (part == "**" and i and parts[i - 1] == "**")
        ]
        # Walked here: every segment through the last one that leads to a
        # directory to search, and a final ``..``, which pathlib would take
        # from whatever directory it names.
        walked = 0
        for i, part in enumerate(parts):
            if part != "**" and (i < len(parts) - 1 or part == ".."):
                walked = i + 1
        if not walked:
            return list(start.glob(pattern))

        dirs = [start]
        for part in parts[:walked]:
            found: Iterable[Path]
            if part == "..":
                found = (d / ".." for d in dirs)
            elif part == "**":
                # Zero or more directories, not through links, as pathlib's
                # ``**`` descends.
                found = (Path(top) for d in dirs for top, _, _ in os.walk(d))
            else:
                found = (match for d in dirs for match in d.glob(part))
            dirs = [d for d in found if d.is_dir() and self._resolves_inside(d)]

        rest = "/".join(parts[walked:])
        if not rest:
            return dirs
        if pattern.endswith("/"):
            rest += "/"
        return [match for d in dirs for match in d.glob(rest)]

    def _resolves_inside(self, path: Path) -> bool:
        """Whether ``path`` resolves inside the root, the check
        ``_validate_path`` makes, as a yes or no."""
        try:
            Path(os.path.realpath(path)).relative_to(self.root)
        except (OSError, ValueError):
            return False
        return True

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
                raise PermissionError(f"Path escapes sandbox: '{path_str}'")

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

            # The root itself and a path ending in ".." have no name to keep
            # unresolved: what they name is a directory, and it must resolve
            # inside. Taken apart, the root's parent is outside it, and
            # "/.." is the root's parent with a name of "..".
            if host_path == self.root or host_path.name == "..":
                return self._validate_path(path)

            # Resolve only the parent, keep the final component unresolved
            parent_resolved = host_path.parent.resolve()
            try:
                parent_resolved.relative_to(self.root)
            except ValueError:
                raise PermissionError(f"Path escapes sandbox: '{path_str}'")

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

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        """Read a file as bytes, or a byte range of it.

        The defaults read the whole file. A range seeks the host file and
        reads only what was asked for, so reading the last kilobyte of a
        gigabyte costs a kilobyte; a read at or past the end returns ``b""``
        and one running past the end is truncated there.

        Raises:
            ValueError: If offset is negative.
        """
        if offset < 0:
            raise ValueError(f"negative read offset: {offset}")
        with suspend():
            resolved = self._validate_path(path)
            if offset == 0 and size < 0:
                return resolved.read_bytes()
            with io.open(resolved, "rb") as handle:
                handle.seek(offset)
                return handle.read() if size < 0 else handle.read(size)

    def write(self, path: str, content: bytes, mode: str = "w") -> None:
        """Write bytes to file, creating parent directories if needed.

        Args:
            path: File path to write.
            content: Bytes to write.
            mode: Write mode ('w' for write, 'a' for append).
        """
        with suspend():
            resolved = self._validate_path(path)
            try:
                resolved.parent.mkdir(parents=True, exist_ok=True)
            except FileExistsError:
                # The parent is there and is not a directory: a file on the
                # way, which open(2) reports as ENOTDIR (as it does when the
                # file is further up and mkdir reaches it first).
                raise NotADirectoryError(
                    errno.ENOTDIR, os.strerror(errno.ENOTDIR), path
                ) from None

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
        """Remove a file, or a symlink itself rather than what it points at.

        ``unlink()`` never follows the final component -- removing a link is
        how a link is removed. Resolving it first deleted the *target*: with
        ``shutil.rmtree()`` correctly declining to recurse into a symlinked
        directory, it unlinks the link instead, and a resolving remove() turned
        that back into the deletion of a tree outside the one being removed.
        """
        with suspend():
            unresolved = self._validate_path_no_follow(path)
            if unresolved.is_symlink():
                unresolved.unlink()
                return

            resolved = self._validate_path(path)
            if resolved.is_dir():
                raise IsADirectoryError(f"Is a directory: {path}")
            resolved.unlink()

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        mode: int = 0o777,
    ) -> None:
        """Create a directory.

        ``mode`` is an IsolatedFS extension beyond the FileSystem protocol and
        only applies to direct calls -- patched ``os.mkdir`` does not forward
        it, so directories created through the patch layer get the default
        permissions.
        """
        with suspend():
            resolved = self._validate_path(path)
            resolved.mkdir(mode=mode, parents=parents, exist_ok=exist_ok)

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

    def utime(
        self,
        path: str,
        times: tuple[float, float] | None = None,
    ) -> None:
        """Set access and modification times; ``None`` means now.

        The last of the optional methods the patch layer dispatches to that
        IsolatedFS did not implement, so ``os.utime()``, ``Path.touch()`` on an
        existing file, and everything built on ``shutil.copystat()`` --
        ``copy2()``, ``copytree()`` -- raised ``NotImplementedError`` against
        an isolated root.
        """
        with suspend():
            resolved = self._validate_path(path)
            os.utime(resolved, times)

    def access(self, path: str, mode: int) -> bool:
        """Check file access permissions."""
        with suspend():
            resolved = self._validate_path(path)
            return os.access(resolved, mode)

    def readlink(self, path: str) -> str:
        """Read a symbolic link target, in this filesystem's namespace.

        Every path this filesystem hands back is virtual -- root-relative,
        with the root reported as "/" -- and a link target is no
        exception. An absolute target is stored on disk as a host path,
        because that is what the kernel needs, but the host prefix is not
        the caller's to see: code inside a patch() context would read the
        sandbox's real location out of it, and a wrapper composing this
        filesystem would be handed a path it cannot route. So an absolute
        target is reported relative to the root, which is the form
        symlink() accepts back -- the two are inverses.

        A relative target resolves against the link's own directory, so
        it names the same file whichever namespace reads it, and is
        returned unchanged.
        """
        with suspend():
            unresolved = self._validate_path_no_follow(path)
            target = os.readlink(unresolved)
            # The target must resolve inside the root, whichever way it is
            # spelled: an absolute one under the root can still climb out
            # of it with ".." or through a link (joining an absolute target
            # onto the link's directory gives the target).
            target_path = Path(target)
            escapes = PermissionError(f"Symlink target escapes sandbox: '{path}'")
            if not self._resolves_inside(unresolved.parent / target_path):
                raise escapes
            try:
                if target_path.is_absolute():
                    inside = target_path.relative_to(self.root)
                else:
                    inside = None
            except ValueError:
                raise escapes

            if inside is None:
                return target
            relative = inside.as_posix()
            return "/" if relative == "." else "/" + relative

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
        """Get metadata for all files and directories by walking the root.

        A link is in it with its target's metadata when ``stat()`` would
        give that, and left out when ``stat()`` would refuse or fail: a
        link out of the root, a dangling one, a loop.
        """
        with suspend():
            result = {}
            for item in self.root.rglob("*"):
                rel_path = str(item.relative_to(self.root))
                st = self._stat_entry(item)
                if st is None:
                    continue
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

    def read_many(self, paths: Iterable[str]) -> dict[str, bytes]:
        """Read several whole files: path (as given) to bytes, for the
        paths that are files. A path that is not one is left out."""
        found: dict[str, bytes] = {}
        for path in paths:
            # Regular files only (through a link, as ``isfile`` follows
            # one): a socket cannot be read and a FIFO would block
            # waiting for a writer.
            if not self.isfile(path):
                continue
            try:
                found[path] = self.read(path)
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                continue  # gone between the check and the read
        return found

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

        Each ``FileInfo.path`` is the directory as it was asked for, joined
        with the entry's path relative to it: listing ``"/src"`` names
        ``"/src/lib/util.py"`` and listing ``"src"`` names ``"src/lib/util.py"``.
        So an absolute query answers in absolute paths and a relative one
        answers relative to the same place the caller named, and neither ever
        carries the host location of the root -- what comes back is a path
        this filesystem accepts back.

        The details are what ``stat()`` reports, so an entry it has none
        for is left out, as ``os.scandir()`` leaves it out under
        ``patch()``: a link out of the root, whose target's details are
        not this filesystem's to give, a dangling link, a loop.

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
            prefix = path.rstrip("/")

            for item in items:
                relative = item.relative_to(resolved).as_posix()
                display = f"{prefix}/{relative}" if prefix != "." else relative
                stat_info = self._stat_entry(item)
                if stat_info is None:
                    continue

                result.append(
                    FileInfo(
                        name=item.name,
                        path=display,
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

    def _stat_entry(self, item: Path) -> os.stat_result | None:
        """``item.stat()`` for an entry of a directory inside the root, or
        ``None`` where ``stat()`` would refuse or fail on it.

        The directory is inside, so only a link can lead out of it.
        """
        if item.is_symlink() and not self._resolves_inside(item):
            return None
        try:
            return item.stat()
        except OSError:
            return None
