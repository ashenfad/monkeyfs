# API Reference

## Context managers

### `patch(fs)`

Activate filesystem interception. All stdlib file operations within the block route through `fs`:

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

### `defer_commits()`

Suppress per-mutation commits to the VirtualFS backing store:

```python
from monkeyfs import VirtualFS, defer_commits, patch

store = MyPersistentMapping()  # any MutableMapping with a commit() method
vfs = VirtualFS(store)

with defer_commits():
    with patch(vfs):
        for i in range(1000):
            with open(f"file_{i}.txt", "w") as f:
                f.write(str(i))

store.commit()  # persist all writes at once
```

VirtualFS accepts any `MutableMapping[str, bytes]` as its state. If the mapping also has a `commit()` method (e.g. gitkv, shelve), VirtualFS calls it after each mutation so changes are persisted immediately. `defer_commits()` suppresses those calls, letting you batch writes and commit once at the end. This avoids expensive per-write persistence during bulk operations.

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

Directories are implicit (inferred from file paths, like S3) but can also be created explicitly with `mkdir()`.

### `IsolatedFS(root, state)`

Real filesystem restricted to a root directory. All paths are resolved within the root; attempts to escape via `..` or symlinks raise `PermissionError`. `state` is a `MutableMapping` for metadata tracking.

```python
isolated = IsolatedFS(root="/tmp/sandbox", state={})
with patch(isolated):
    open("/etc/passwd")  # PermissionError
```

### `VirtualFile`

File object returned by `VirtualFS.open()`. Implements the standard file protocol (`read`, `write`, `seek`, `close`, etc.).

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
- `IsolatedFSConfig` -- config for IsolatedFS (root directory, tracking)

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
| `os` | `listdir`, `scandir`, `remove`, `unlink`, `mkdir`, `makedirs`, `rmdir`, `rename`, `replace`, `stat`, `lstat`, `getcwd`, `chdir`, `utime`, `getenv`, `access`, `readlink`, `symlink`, `link`, `chmod`, `chown`, `truncate` |
| `os.path` | `exists`, `isfile`, `isdir`, `islink`, `lexists`, `samefile`, `realpath`, `abspath`, `getsize`, `expanduser`, `expandvars` |
| `pathlib` | `Path.touch`, `Path._globber` (3.13+) |
| `glob` | `_StringGlobber` (3.13+) |
| `fcntl` | `fcntl`, `flock`, `lockf` (no-op under VFS; Posix only) |
| `shutil` | Optimization flags disabled during `patch()` to force string-path code paths |

## Known limitations

- **`tempfile`** -- `mkstemp()`, `NamedTemporaryFile()`, etc. use `os.open()` (low-level C syscall) which bypasses VFS patches. Use `open()` with explicit paths instead.
