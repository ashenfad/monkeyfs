# API Reference

- [Context managers](#context-managers) -- `patch`, `suspend`
- [Filesystem implementations](#filesystem-implementations) -- `VirtualFS`, `IsolatedFS`
- [Protocol & types](#protocol--types) -- `FileSystem`, `FileMetadata`, `FileInfo`
- [Configuration](#configuration) -- `connect_fs`, config dataclasses
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

Uses `contextvars` so concurrent async tasks each get their own filesystem. Nests correctly -- inner `patch()` blocks override the outer one and restore on exit.

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

### `IsolatedFS(root)`

Real filesystem restricted to a root directory. All paths are resolved within the root; attempts to escape via `..` or symlinks raise `PermissionError`.

```python
isolated = IsolatedFS(root="/tmp/sandbox")
with patch(isolated):
    open("/etc/passwd")  # PermissionError
```

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
islink(path) -> bool            # os.path.islink
samefile(p1, p2) -> bool        # os.path.samefile
realpath(path) -> str           # os.path.realpath
getsize(path) -> int            # os.path.getsize
replace(src, dst) -> None       # os.replace
access(path, mode) -> bool      # os.access
readlink(path) -> str           # os.readlink
symlink(src, dst) -> None       # os.symlink
link(src, dst) -> None          # os.link
chmod(path, mode) -> None       # os.chmod
chown(path, uid, gid) -> None   # os.chown
truncate(path, length) -> None  # os.truncate
```

### termish compatibility

Both `VirtualFS` and `IsolatedFS` satisfy the [termish](https://github.com/ashenfad/termish) `FileSystem` protocol, which covers direct-use methods (`read`, `write`, `list_detailed`, `glob`, etc.) beyond the patching surface above. This means either can be passed directly to termish's terminal interpreter for shell command execution over the virtual filesystem.

### `FileMetadata`

Dataclass returned by `stat()`. Fields: `size`, `created_at`, `modified_at`, `is_dir`. Also exposes `os.stat_result`-compatible properties (`st_size`, `st_mode`, `st_mtime`, etc.).

### `FileInfo`

Dataclass for UI display. Fields: `name`, `path`, `size`, `created_at`, `modified_at`, `is_dir`.

## Configuration

### `connect_fs(type, ...)`

Factory that builds a filesystem from a config dict or keyword arguments:

```python
from monkeyfs import connect_fs

fs = connect_fs(type="virtual")
fs = connect_fs(type="isolated", root="/tmp/sandbox")
```

### Config dataclasses

- `FSConfig` -- base config
- `VirtualFSConfig` -- config for VirtualFS (size limits, etc.)
- `IsolatedFSConfig` -- config for IsolatedFS (root directory)

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
| `os` | `listdir`, `scandir`, `remove`, `unlink`, `mkdir`, `makedirs`, `rmdir`, `rename`, `replace`, `stat`, `lstat`, `getcwd`, `chdir`, `utime`, `getenv`, `access`, `readlink`, `symlink`, `link`, `chmod`, `chown`, `truncate`, `open`, `read`, `write`, `close`, `fstat`, `lseek` |
| `os.path` | `exists`, `isfile`, `isdir`, `islink`, `lexists`, `samefile`, `realpath`, `abspath`, `getsize`, `expanduser`, `expandvars` |
| `pathlib` | `Path.touch`, `Path._globber` (3.13+) |
| `glob` | `_StringGlobber` (3.13+) |
| `fcntl` | `fcntl`, `flock`, `lockf` (no-op under VFS; Posix only) |
| `shutil` | Optimization flags disabled during `patch()` to force string-path code paths |
| `tempfile` | `tempdir` reset during `patch()` so temp paths resolve inside VFS; `_TemporaryFileCloser` unlink re-bound for `delete=True` cleanup |

## Known limitations

- **C-level syscalls** -- Libraries that call the OS directly from C extensions (e.g. SQLite, `mmap`) bypass Python-level patches entirely. Only Python-level file operations are intercepted.
- **`fcntl` locking** -- `fcntl`, `flock`, and `lockf` are no-ops under VFS since virtual files have no real file descriptors. Code that depends on advisory locking semantics will not see contention.
