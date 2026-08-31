# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Overlapping `patch()` contexts no longer un-patch each other**: `patch()` saved and restored `tempfile.tempdir` and shutil's `_use_fd_functions`, `_HAS_FCOPYFILE`, `_USE_CP_SENDFILE`, `_USE_CP_COPY_FILE_RANGE` and (3.14+) `_rmtree_impl` in local variables, as if they were per-context like `current_fs`. They are not -- shutil and tempfile read them from their own module namespace, so they are process-global. Two overlapping contexts (concurrent threads, or async tasks in different contexts) therefore had the second one save the *already-patched* values, and the first one to exit restored the originals while the second was still live: that context silently regained shutil's fd-based code paths, where `rmtree` traverses by file descriptor and no longer routes through the active filesystem. Interleaved exits also left the patched values installed process-wide after the last context was gone. The shims are now reference counted under a lock -- the first entry saves the originals, the last exit restores them -- so they hold for as long as any context is live. Because they remain process-global, they are also visible to code running outside `patch()` while another context is active; noted in the docs.

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
