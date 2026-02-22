# monkeyfs 🐒

Transparent filesystem interception via monkey-patching.

Patches `open()`, `os.listdir()`, `os.stat()`, and 30+ other stdlib functions to route through a virtual or isolated filesystem. Patches are installed at import time and are inert until activated with `use_fs()`. Uses `contextvars` for async-safe isolation between concurrent tasks. Zero dependencies.

## Features

- **VirtualFS** -- virtual filesystem backed by any `MutableMapping[str, bytes]` (dict, database, etc.) with metadata tracking
- **MemoryFS** -- lightweight in-memory filesystem using plain dicts (`str | bytes` values)
- **IsolatedFS** -- real filesystem restricted to a root directory with path-escape prevention
- **Transparent patching** -- `open()`, `os.*`, `os.path.*`, `pathlib.Path` all intercepted automatically
- **Async-safe** -- each async task gets its own filesystem context via `contextvars`
- **System path passthrough** -- stdlib and site-packages remain readable even under interception
- **FileSystem protocol** -- structural typing; any object with the right methods works

## Install

```bash
pip install monkeyfs
```

## Quick example

```python
from monkeyfs import VirtualFS, use_fs

vfs = VirtualFS({})

with use_fs(vfs):
    # Standard Python I/O is transparently intercepted
    with open("data.csv", "w") as f:
        f.write("name,score\nalice,98\nbob,87\n")

    import os
    print(os.listdir("/"))        # ['data.csv']
    print(os.path.getsize("data.csv"))  # 30

    with open("data.csv") as f:
        print(f.read())           # name,score\nalice,98\nbob,87\n
```

## IsolatedFS

Restricts file operations to a root directory on the real filesystem:

```python
from monkeyfs import IsolatedFS, use_fs

isolated = IsolatedFS(root="/tmp/sandbox", state={})

with use_fs(isolated):
    with open("notes.txt", "w") as f:
        f.write("hello")          # Written to /tmp/sandbox/notes.txt

    open("/etc/passwd")           # PermissionError -- outside root
```

## FileSystem protocol

Any object implementing the right methods works with monkeyfs -- no inheritance required. Not every method is needed; the patching layer checks at call time and raises `NotImplementedError` for anything missing.

**Required** -- patching will fail without these:

```python
open(path, mode="r", **kwargs) -> Any
stat(path) -> FileMetadata
exists(path) -> bool
isfile(path) -> bool
isdir(path) -> bool
list(path=".") -> list[str]     # or listdir()
remove(path) -> None
mkdir(path, parents=False, exist_ok=False) -> None
makedirs(path, exist_ok=True) -> None
rename(src, dst) -> None
getcwd() -> str
chdir(path) -> None
```

**Optional** -- `NotImplementedError` raised if the corresponding stdlib function is called but the method is missing:

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

## Patched functions

| Module | Functions |
|--------|-----------|
| `builtins` / `io` | `open` |
| `os` | `listdir`, `scandir`, `remove`, `unlink`, `mkdir`, `makedirs`, `rmdir`, `rename`, `replace`, `stat`, `lstat`, `getcwd`, `chdir`, `utime`, `getenv`, `access`, `readlink`, `symlink`, `link`, `chmod`, `chown`, `truncate` |
| `os.path` | `exists`, `isfile`, `isdir`, `islink`, `lexists`, `samefile`, `realpath`, `abspath`, `getsize`, `expanduser`, `expandvars` |
| `pathlib` | `Path.touch`, `Path._globber` (3.13+) |
| `glob` | `_StringGlobber` (3.13+) |
| `fcntl` | `fcntl`, `flock`, `lockf` (no-op under VFS; Posix only) |
| `shutil` | Optimization flags disabled during `use_fs()` to force string-path code paths |

## Known limitations

- **`tempfile.mkstemp()` / `NamedTemporaryFile()`** use `os.open()` (the low-level C syscall), not `builtins.open()`. Patching `os.open` is impractical — too many internal callers depend on it. Temporary files created this way will hit the real filesystem even inside `use_fs()`. Use `open()` with explicit paths instead.

## Development

```bash
uv sync --extra dev
uv run pytest
```

## License

MIT
