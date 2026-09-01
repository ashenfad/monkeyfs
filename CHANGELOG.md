# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security
- **Unpatched `os` functions carried virtual paths to the real filesystem.** `os.chflags()`, `os.lchmod()`, `os.lchflags()` and the four extended-attribute functions were never patched, so with a filesystem active they operated on the host: `os.lchmod("/etc/passwd", 0o600)` inside `patch()` changed a real file's mode, silently. All are shimmed now. The BSD-only ones could never have been caught -- CI ran ubuntu only, where they do not exist -- so macOS jobs were added first, to verify the fix where the functions are real.
- **`Path.chmod()` escaped to the host on Python 3.10.** `pathlib._NormalAccessor.chmod` is a reference to `os.chmod` captured at import, and `install()` re-bound ten of that class's attributes but never `chmod`. 3.11 removed the accessor and is unaffected. **Still open:** `replace`, `link`, `symlink` and `readlink` are captured in the same accessor and remain unpatched.
- **Overlapping `patch()` contexts un-patched each other.** The `shutil` and `tempfile` compatibility shims are process-global but were saved and restored per-context, so one context exiting re-enabled `shutil`'s fd-based code paths -- which bypass the active filesystem -- underneath another still running. They are reference counted now, and stay applied while any context is live, including for code outside `patch()`.

### Changed
- **`follow_symlinks=False` on a symlink now raises `ENOTSUP`** for `os.chmod()` and `os.utime()`, rather than silently modifying the link's target. No backend can set a link's own mode or times, so both available behaviours were wrong; this follows the reasoning `_reject_dir_fd()` already applies. The refusal is narrow -- the flag must be `False` *and* the path a symlink. Not BSD-only: `Path.lchmod()` routes through `os.chmod` on every platform.
- **`shutil.copy(src, dst, follow_symlinks=False)` between two symlinks now raises on macOS.** `shutil.copymode()` calls `os.lchmod()` with no exception handling in any supported version, so the refusal above surfaces. The alternative was reporting success for a permission change nothing performed.
- **BSD flags and extended attributes are refused rather than passed through.** `os.chflags()`, `os.lchflags()`, `os.setxattr()` and `os.removexattr()` raise `ENOTSUP`; `os.listxattr()` returns `[]` and `os.getxattr()` raises `ENODATA` -- the answers a filesystem holding none would give. Every errno is one `shutil.copystat()` tolerates on 3.10 through 3.14.
- **`os.scandir()` entries and `os.lstat()` report symlinks as symlinks.** `MockDirEntry.is_symlink()` was hardcoded `False`, `is_dir()`/`is_file()`/`stat()` accepted `follow_symlinks=` and ignored it, and `os.lstat()` delegated to `os.stat()`. Link status now comes from the backend's optional `islink()`, captured when the entry is produced; `os.lstat()` on a link synthesizes `S_IFLNK` and the target string's length. Backends without `islink()` report no links and behave exactly as before.
- **CI runs on macOS as well as ubuntu, and stopped running every matrix twice.** ubuntu keeps 3.10-3.14; macOS runs 3.10 and 3.14, the two ends of the version-conditional patching in `install()`. `fail-fast` is off and pushes to any branch are tested. The concurrency group previously resolved to `Tests-refs/heads/foo` on a push and `Tests-foo` on the pull_request run for the same commit, so nothing cancelled and fourteen jobs ran per push; it now normalizes on the bare branch name and includes the head repository, so forks cannot cancel each other. Windows remains untested and unclaimed.

### Fixed
- **`shutil.rmtree()` deleted through symlinks**, destroying directories outside the tree it was given: `rmtree("/tree")` on a tree containing `tree/inward -> sibling` removed `sibling` and everything in it. Two independent causes, each sufficient on its own -- entries not reporting link status, and `IsolatedFS.remove()` resolving the final component, so unlinking a link deleted its target instead.
- **`shutil.rmtree()` raised `AttributeError` on any tree containing a subdirectory**, removing nothing. `MockDirEntry` had no `is_junction()`, which CPython 3.12+ calls for every entry it has decided is a directory -- and which `patch()` steers callers onto by forcing the string-path implementation. It answers `False`: junctions are a Windows construct with no equivalent in the protocol. That crash had been masking the symlink deletion above.
- **`copy2()` and `copytree()` stamped every destination with the current time.** `shutil.copystat()` passes `ns=` and never `times=`, and the shim forwarded only `times`. `ns` is now converted at the patch boundary rather than added to the optional-method contract, costing sub-microsecond fidelity no backend could hold. Honoring it was necessary but not sufficient: an `os.stat_result` built from a bare 10-tuple answers `None` for `st_atime_ns`/`st_mtime_ns`/`st_ctime_ns`, so the source timestamp was `None` -- the shim supplies all three now. The existing `copy2()`/`copytree()` tests passed for the whole life of the bug because none asserted a timestamp.
- **`copy2()`, `copytree()` and `Path.touch()` raised `NotImplementedError` against an `IsolatedFS`**, the one optional method it never implemented.
- **Unpatched shims invented arguments the caller omitted.** `_vfs_chmod` and the four xattr shims declared `follow_symlinks: bool = True` and forwarded it unconditionally, so outside `patch()` a bare call was not identical to the function it replaced. All six now share one `_UNSET` sentinel and forward only what was passed. No behaviour change on any supported platform -- these defaults are `True` on POSIX -- so this is the inert-when-unpatched invariant being made to hold, not a bug being fixed.

## [0.1.6] - 2026-08-04

### Fixed
- **`VirtualFS.open()` update modes**: `r+` and `rb+` now support reading and persist in-place writes instead of silently discarding them.
- **Safe system path boundaries**: Read-only host passthrough now requires an actual path relationship, so a trusted path such as `/usr/local` no longer also trusts prefix-sharing siblings such as `/usr/local-private`.
- **`bytes` and `os.PathLike` paths no longer bypass interception**: `open()` and `os.open()` matched only `str` and `pathlib.Path`, so any other path type the stdlib accepts skipped the filesystem entirely and reached the host -- `open(b"/etc/passwd").read()` was a plain read bypass, and a bytes write landed on the real filesystem. Paths are now normalized with `os.fsdecode()` before routing, so `bytes` and non-`Path` `os.PathLike` arguments resolve against the active filesystem like any other path. Non-path arguments (file descriptors) still fall through unchanged.
- **Safe-path passthrough no longer hands out host directory fds**: Read-only opens on safe system paths return a real host fd, which for a directory is a durable capability that outlives the safe-path check that granted it -- and was the only way to obtain the fd that made the `dir_fd` escape below reachable. `os.open()` on a directory now raises `IsADirectoryError` while a filesystem is active. File reads through the safe-path fallback are unchanged, as are `os.listdir()` and `os.scandir()`.
- **`dir_fd` no longer escapes the filesystem boundary**: `os.rmdir()` and `os.open()` honored a `dir_fd`, resolving against the host filesystem and bypassing the active filesystem entirely -- including `os.open`'s safe-path and write filters, since its `dir_fd` branch ran ahead of both. A real directory fd is obtainable from inside a patched context via the read-only safe-system-path passthrough, so this chained into arbitrary host reads and writes. Every other patched operation silently dropped `dir_fd`, retargeting the call at a different directory than the caller named. Both behaviors are now replaced by `OSError(errno.ENOTSUP)` for `dir_fd`, `src_dir_fd`, and `dst_dir_fd` across all patched operations while a filesystem is active; outside `patch()`, `dir_fd` is unaffected.
- **Patched `os.mkdir` no longer forwards `mode`**: The `FileSystem` protocol declares `mkdir(path, parents, exist_ok)`, but the patch layer passed an undeclared `mode=` kwarg, so any backend implementing the interface as documented raised `TypeError` on `os.mkdir()` / `Path.mkdir()`. `mode` is now dropped at the patch boundary, matching what `os.makedirs` already did. `VirtualFS.mkdir()` drops its unused `mode` parameter; `IsolatedFS.mkdir()` keeps it for direct calls, but directories created through patched `os.mkdir` now get default permissions instead of the caller's requested mode.

## [0.1.5] - 2026-07-06

### Added
- **`VirtualFS.invalidate()`**: Public API to drop the lazy dir/metadata/size caches after the backing state mutates externally (e.g. a versioned store rolled back underneath a live instance). Replaces reaching into private attributes.

### Fixed
- **Unclosed `VirtualFile` data loss**: `VirtualFile` gains `__del__` — buffered content now persists at garbage collection with a `ResourceWarning`, matching real file semantics. Previously `open(p, 'w').write(x)` without `close()` silently lost the write.

## [0.1.4] - 2026-03-12

### Fixed
- **`list_detailed` path consistency**: `FileInfo.path` now preserves the caller's queried prefix across all implementations — `list_detailed("src/")` returns `path="src/lib/util.py"`, `list_detailed("/")` returns `path="/src/lib/util.py"`, and `list_detailed(".")` returns `path="src/lib/util.py"`. Previously MountFS stripped the queried prefix and VirtualFS stripped the leading slash for root queries.

## [0.1.3] - 2026-03-08

### Changed
- **Pyodide compatibility**: Minor adjustments for pyodide compat (os.link)

## [0.1.2] - 2026-03-03

### Added
- **ReadOnlyFS**: Wrapper that blocks all write operations with `PermissionError`. Read operations delegate transparently to the wrapped filesystem.
- **MountFS**: Routes filesystem operations to different backing filesystems by path prefix. Supports dynamic mount/unmount, own CWD, list merging at mount boundaries, cross-mount rename, nested mounts, and glob across mounts.

## [0.1.1] - 2026-02-28

### Fixed
- **VFS metadata serialization**: Switched from pickle to JSON
- **stat() on files without metadata**: Returns synthetic metadata instead of KeyError
- **stat() synthetic size**: Use actual file size instead of 0 for files without metadata entries
- **utime**: Updates VFS metadata instead of silently no-oping
- **readlink**: Validates relative symlink targets stay within sandbox root
- **mkdir mode**: Passes mode argument through to IsolatedFS
- **realpath escape fallback**: Return normalized absolute path instead of "/" when path escapes sandbox

### Changed
- **Metadata caching**: Parsed metadata cached in memory to avoid repeated JSON deserialization
- **remove_many**: Batched metadata update instead of one per file
- **connect_fs removed**: Deferred filesystem config moved to agex where it belongs
- **Backing state ownership**: Documented that VFS backing state should be treated as owned by the instance
