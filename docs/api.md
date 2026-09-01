# API Reference

- [Context managers](#context-managers) -- `patch`, `suspend`
- [Filesystem implementations](#filesystem-implementations) -- `VirtualFS`, `IsolatedFS`, `ReadOnlyFS`, `MountFS`
- [Protocol & types](#protocol--types) -- `FileSystem`, `FileMetadata`, `FileInfo`
- [Low-level](#low-level) -- `current_fs`
- [Patched functions](#patched-functions)
- [Known limitations](#known-limitations)

## Context managers

### `patch(fs)`

Activate filesystem interception. All stdlib file operations within the block route through `fs`. Patches are applied lazily on first call and remain inert (falling through to originals) outside the context:

```python
from monkeyfs import VirtualFS, patch

vfs = VirtualFS({})
with patch(vfs):
    with open("file.txt", "w") as f:
        f.write("hello")
```

Uses `contextvars` so concurrent async tasks each get their own filesystem. Nests correctly -- inner `patch()` blocks override the outer one and restore on exit. The `shutil` and `tempfile` shims are process-global rather than per-context and are reference counted: they stay applied while any `patch()` context is live anywhere in the process, and are restored when the last one exits (see [Known limitations](#known-limitations)).

### `suspend()`

Temporarily bypass interception and access the real filesystem:

```python
from monkeyfs import VirtualFS, patch, suspend

vfs = VirtualFS({})
with patch(vfs):
    with suspend():
        import os
        print(os.getcwd())  # actual working directory, not "/"
```

Useful for privileged functions that need host filesystem access while patching is active.

## Filesystem implementations

### `VirtualFS(state)`

In-memory virtual filesystem. `state` is any `MutableMapping[str, bytes]` -- a plain `dict`, a database-backed mapping, etc.

```python
vfs = VirtualFS({})
vfs.write("file.txt", b"content")
vfs.read("file.txt")          # b"content"
vfs.exists("file.txt")        # True
vfs.list("/")                  # ["file.txt"]
```

**Directory model:** Directories can be created explicitly with `mkdir()` or implicitly -- any file path like `a/b/file.txt` makes `a/` and `a/b/` visible to `isdir()`, `list()`, and `exists()`. The direct `vfs.write()` method auto-creates parent directories for convenience; patched `open()` does not (raises `FileNotFoundError` on missing parents, matching POSIX). `rmdir()` follows POSIX semantics -- fails on non-empty directories regardless of how they were created.

**Buffering:** Files opened for writing buffer all content in memory and persist to the backing state on `close()`. `flush()` is a no-op -- there is no incremental persistence. This matches how most in-memory filesystems work but differs from real filesystems where `flush()` pushes data to the OS. For the fd emulation layer (`os.open`/`os.write`), the same applies: content is flushed to VFS on `os.close()`.

**Backing state ownership:** VirtualFS caches parsed metadata in memory for performance. The backing `MutableMapping` should be treated as owned by the VFS instance -- external mutations to the state while the VFS is active may not be reflected.

### `IsolatedFS(root)`

Real filesystem restricted to a root directory. All paths are resolved within the root; attempts to escape via `..` or symlinks raise `PermissionError`.

```python
isolated = IsolatedFS(root="/tmp/sandbox")
with patch(isolated):
    open("/etc/passwd")  # PermissionError
```

### `ReadOnlyFS(fs)`

Wraps any filesystem and blocks all write operations with `PermissionError`. Read operations delegate transparently via `__getattr__`.

```python
from monkeyfs import VirtualFS, ReadOnlyFS

vfs = VirtualFS({})
vfs.write("data.csv", b"a,b,c")

ro = ReadOnlyFS(vfs)
ro.read("data.csv")       # b"a,b,c"
ro.write("x.txt", b"hi")  # PermissionError: Read-only filesystem
```

**Blocked operations:** `open` (write/append/exclusive modes), `write`, `write_many`, `remove`, `remove_many`, `mkdir`, `makedirs`, `rename`, `rmdir`, `replace`, `symlink`, `link`, `chmod`, `chown`, `truncate`.

**Allowed operations:** All read operations delegate transparently -- `open` (read mode), `read`, `stat`, `exists`, `isfile`, `isdir`, `list`, `glob`, `getcwd`, `chdir`, etc.

**`access()`:** Returns `False` for `os.W_OK`, delegates for `os.R_OK` and `os.F_OK`.

### `MountFS(base, mounts=None)`

Routes filesystem operations to different backing filesystems based on path prefix. `base` handles all paths not covered by a mount. `mounts` is a dict mapping absolute path prefixes to filesystems.

```python
from monkeyfs import VirtualFS, MountFS, ReadOnlyFS

base = VirtualFS({})
chapters = VirtualFS({})
chapters.write("summary.md", b"# Chapter 1")

fs = MountFS(base, {"/chapters": ReadOnlyFS(chapters)})
fs.read("/chapters/summary.md")    # b"# Chapter 1"
fs.write("/app.py", b"print(1)")   # goes to base
fs.write("/chapters/x.md", b"no")  # PermissionError (read-only mount)
```

**Dynamic mounts:** `fs.mount("/data", some_fs)` and `fs.unmount("/data")`.

**CWD:** MountFS maintains its own working directory (default `"/"`). Relative paths are resolved against it before routing.

**Directory listing:** `list()` at a directory containing mount points merges entries from the base filesystem with mount-point names. Recursive listing includes contents of mounted filesystems.

**Mount-point existence:** Mount prefixes (and their implicit parents) report as existing directories via `isdir()`, `exists()`, and `stat()`.

**Cross-mount rename:** Files are copied then removed. Directory renames across mount boundaries raise `OSError(errno.EXDEV)`, matching POSIX cross-device semantics.

**Nested mounts:** Supported. A mount at `/a/b` takes priority over `/a` for paths under `/a/b/`.

## Protocol & types

### `FileSystem` (Protocol)

Structural typing -- any object with the right methods works, no inheritance required. The patching layer checks at call time and raises `NotImplementedError` for anything missing.

**Required methods:**

```python
open(path, mode="r", **kwargs) -> Any
stat(path) -> FileMetadata
exists(path) -> bool
isfile(path) -> bool
isdir(path) -> bool
list(path=".") -> list[str]
remove(path) -> None
mkdir(path, parents=False, exist_ok=False) -> None
makedirs(path, exist_ok=True) -> None
rename(src, dst) -> None
getcwd() -> str
chdir(path) -> None
```

**Optional methods** -- `NotImplementedError` raised if the corresponding stdlib function is called but the method is missing:

```python
rmdir(path) -> None             # os.rmdir
islink(path) -> bool            # os.path.islink, os.scandir, os.lstat
utime(path, times) -> None      # os.utime, Path.touch
samefile(p1, p2) -> bool        # os.path.samefile
realpath(path) -> str           # os.path.realpath
getsize(path) -> int            # os.path.getsize
replace(src, dst) -> None       # os.replace
access(path, mode) -> bool      # os.access
readlink(path) -> str           # os.readlink
symlink(src, dst) -> None       # os.symlink
link(src, dst) -> None          # os.link
chmod(path, mode) -> None       # os.chmod, os.lchmod
chown(path, uid, gid) -> None   # os.chown
truncate(path, length) -> None  # os.truncate
```

`utime()` keeps the `(path, times)` signature above even though `os.utime()` is reached almost entirely through `ns=` -- `shutil.copystat()`, and with it `copy2()` and `copytree()`, calls `os.utime(dst, ns=(atime_ns, mtime_ns))` and never passes `times`. The `ns` pair is converted to seconds at the patch boundary instead of being handed to the backend, so a backend written to the interface above needs no change. The conversion goes through a float, which resolves to roughly 240ns for a present-day timestamp; no in-tree backend stores finer than that in any case (`VirtualFS` keeps an ISO-8601 string, `IsolatedFS` forwards `times` to the host `os.utime()`).

### termish compatibility

`VirtualFS`, `IsolatedFS`, `ReadOnlyFS`, and `MountFS` all satisfy the [termish](https://github.com/ashenfad/termish) `FileSystem` protocol, which covers direct-use methods (`read`, `write`, `list_detailed`, `glob`, etc.) beyond the patching surface above. This means any can be passed directly to termish's terminal interpreter for shell command execution over the virtual filesystem.

### `FileMetadata`

Dataclass returned by `stat()`. Fields: `size`, `created_at`, `modified_at`, `is_dir`. Also exposes `os.stat_result`-compatible properties (`st_size`, `st_mode`, `st_mtime`, etc.).

### `FileInfo`

Dataclass for UI display. Fields: `name`, `path`, `size`, `created_at`, `modified_at`, `is_dir`.

## Low-level

### `current_fs`

The `contextvars.ContextVar` holding the active filesystem (or `None`). This is what `patch()` sets and `suspend()` clears. Useful for inspecting the current state:

```python
from monkeyfs import current_fs

current_fs.get()  # None (no patching active)
```

## Patched functions

| Module | Functions |
|--------|-----------|
| `builtins` / `io` | `open` |
| `os` | `listdir`, `scandir`, `remove`, `unlink`, `mkdir`, `makedirs`, `rmdir`, `rename`, `replace`, `stat`, `lstat`, `getcwd`, `chdir`, `utime`, `getenv`, `access`, `readlink`, `symlink`, `link`, `chmod`, `lchmod`, `chflags`, `lchflags` (last three BSD/macOS), `chown`, `truncate`, `listxattr`, `getxattr`, `setxattr`, `removexattr` (Linux), `open`, `read`, `write`, `close`, `fstat`, `lseek` |
| `os.path` | `exists`, `isfile`, `isdir`, `islink`, `lexists`, `samefile`, `realpath`, `abspath`, `getsize`, `expanduser`, `expandvars` |
| `pathlib` | `Path.touch`, `Path._globber` (3.13+) |
| `glob` | `_StringGlobber` (3.13+) |
| `fcntl` | `fcntl`, `flock`, `lockf` (no-op under VFS; Posix only) |
| `shutil` | Optimization flags disabled while any `patch()` context is live, to force string-path code paths (reference counted across overlapping contexts) |
| `tempfile` | `tempdir` reset while any `patch()` context is live so temp paths resolve inside VFS (reference counted); `_TemporaryFileCloser` unlink re-bound for `delete=True` cleanup |

## Known limitations

- **`bytes` path results** -- `open()` and `os.open()` accept `bytes` and `os.PathLike` paths and normalize them with `os.fsdecode()` before routing to the filesystem. Operations that *return* paths (`os.listdir()`, `os.scandir()`, `os.readlink()`) return `str` regardless of the argument type, where the stdlib would return `bytes` for a `bytes` argument.
- **No host directory fds** -- Read-only opens on safe system paths pass through to the host, but `os.open()` on a *directory* raises `IsADirectoryError` while `patch()` is in effect. A directory fd's only real use is as a `dir_fd` (unsupported, below), and a real kernel handle to a host directory outlives the check that granted it. Use `os.listdir()` / `os.scandir()`, which are virtualized and keep the safe-path fallback. File reads are unaffected.
- **`dir_fd` is unsupported** -- A `dir_fd` (or `src_dir_fd` / `dst_dir_fd`) names a host directory, so the operation would resolve against the real filesystem no matter what the active filesystem says. While `patch()` is in effect, passing one raises `OSError(errno.ENOTSUP)` rather than escaping the filesystem or silently retargeting the call. Outside `patch()`, `dir_fd` behaves normally. Code that probes `os.supports_dir_fd` and falls back to path-based resolution will work unchanged.
- **`shutil` / `tempfile` shims are process-global** -- The active filesystem is per-context, but the shims that keep `shutil` and `tempfile` on their string-path code paths are module attributes those modules read for themselves, so they cannot be scoped to a context. They are reference counted instead: applied while *any* `patch()` context is live and restored when the last one exits. So code running outside `patch()` -- another thread, or a `suspend()` block -- sees them too while some other context is patched, which makes `shutil.rmtree()` on a host path take the slower string-path route. And `tempfile.tempdir` is a single slot, so concurrent contexts share whichever value `tempfile.gettempdir()` resolved first; the path itself still routes through each context's own filesystem.
- **`os.scandir()` entries are snapshots** -- `os.scandir()` yields a stand-in for `os.DirEntry`, not the real thing. Like the real one it answers from values captured when the entry was produced and never re-consults the filesystem, so a tree that changes mid-iteration is described as it was. `is_dir()`, `is_file()` and `is_symlink()` are faithful, and `follow_symlinks=False` is honored: a symlink is not a directory or a file, which is what keeps `shutil.rmtree()` from recursing through a link. Three gaps remain: `is_junction()` is always `False` (a junction is a Windows NTFS construct, and no backend has one), `inode()` is always `0`, and `isinstance(entry, os.DirEntry)` is `False` -- `os.DirEntry` cannot be subclassed, so stdlib fast paths keyed on that check take their generic branch instead.
- **`lstat` results are synthesized** -- No backend exposes an `lstat()`, so `os.lstat()`, `os.stat(follow_symlinks=False)`, `Path.is_symlink()` and `DirEntry.stat(follow_symlinks=False)` build the answer for a symlink from the link and its target: the file type is `S_IFLNK` and `st_size` is the length of the target string, but the timestamps, uid and gid are the *target's*, since the link's own are unreachable. Permission bits are `0o777`, as on Linux. When the target cannot be read at all -- a link out of the sandbox, a backend with no `readlink()` -- these fall back to following the link, so confinement errors still surface. A backend that does not implement `islink()` reports no links at all, and every entry describes its target.
- **`os.chflags()` and `os.lchflags()` refuse** -- BSD file flags are not part of the `FileSystem` protocol, so while `patch()` is in effect both raise `OSError(errno.ENOTSUP)` rather than taking a virtual path to the host. `shutil.copystat()` -- and with it `copy2()` and `copytree()` -- tolerates exactly that errno and carries on. Both functions exist on BSD/macOS only.
- **`os.utime(follow_symlinks=False)` refuses on a symlink** -- The flag means "stamp the link, not its target", and no backend exposes an `lutime()`, so the link's own timestamps are unreachable. Following the link instead would write to the very file the flag exists to protect, so while `patch()` is in effect the call raises `OSError(errno.ENOTSUP)` -- the same answer, for the same reason, as `dir_fd`. The refusal is narrow: it fires only when the flag is `False` *and* the path is a symlink, so ordinary `os.utime()` calls are unaffected. `shutil` never reaches it, because the patched `os.utime` is deliberately left out of `os.supports_follow_symlinks` and `copystat()` substitutes a no-op for anything missing from that set.
- **`os.chmod(follow_symlinks=False)` and `os.lchmod()` refuse on a symlink** -- Both mean "the link's own mode, not its target's", and no backend exposes an `lchmod()`, so the link's permission bits are unreachable; following the link instead would rewrite the mode of the very file the flag exists to protect. While `patch()` is in effect the call raises `OSError(errno.ENOTSUP)`, the same answer `os.utime(follow_symlinks=False)` and `dir_fd` get. The refusal is narrow -- the path must actually be a link -- so `os.chmod()` and `os.lchmod()` on a plain file behave normally. `pathlib.Path.lchmod()` is defined as `chmod(mode, follow_symlinks=False)`, so it lands here too, on every platform. `shutil.copystat()` never trips it (the patched `os.chmod` is deliberately left out of `os.supports_follow_symlinks`), but `shutil.copymode(src, dst, follow_symlinks=False)` between two symlinks does: it calls `os.lchmod()` with no error handling at all in CPython 3.10-3.14, so the `ENOTSUP` surfaces to the caller rather than a mode change being silently dropped. On Linux, where `os.lchmod` does not exist, `copymode()` skips that branch entirely.
- **Extended attributes are absent, not forwarded** -- The xattr family is Linux-only, and no backend stores extended attributes. While `patch()` is in effect, `os.listxattr()` returns `[]` -- the file genuinely has none -- `os.getxattr()` raises `OSError(errno.ENODATA)`, and `os.setxattr()` / `os.removexattr()` raise `OSError(errno.ENOTSUP)` so a write fails loudly instead of being silently dropped. None of them looks at the host, so a virtual path never reaches a real file. `shutil.copystat()` (and `copy2()`, `copytree()`) and `pathlib.Path.copy()` tolerate exactly these errnos and carry on.
- **C-level syscalls** -- Libraries that call the OS directly from C extensions (e.g. SQLite, `mmap`) bypass Python-level patches entirely. Only Python-level file operations are intercepted.
- **`fcntl` locking** -- `fcntl`, `flock`, and `lockf` are no-ops under VFS since virtual files have no real file descriptors. Code that depends on advisory locking semantics will not see contention.
