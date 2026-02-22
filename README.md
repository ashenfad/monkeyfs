# monkeyfs 🐒

Transparent filesystem interception via monkey-patching.

Patches `open()`, `os.listdir()`, `os.stat()`, and 20+ other stdlib functions to route through a virtual or isolated filesystem. Uses `contextvars` for async-safe isolation between concurrent tasks. Zero dependencies.

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

Any object implementing these methods works with monkeyfs -- no inheritance required:

```python
@runtime_checkable
class FileSystem(Protocol):
    def getcwd(self) -> str: ...
    def chdir(self, path: str) -> None: ...
    def glob(self, pattern: str) -> list[str]: ...
    def open(self, path: str, mode: str = "r", **kwargs) -> Any: ...
    def read(self, path: str) -> bytes: ...
    def write(self, path: str, content: bytes, mode: str = "w") -> None: ...
    def exists(self, path: str) -> bool: ...
    def isfile(self, path: str) -> bool: ...
    def isdir(self, path: str) -> bool: ...
    def islink(self, path: str) -> bool: ...
    def lexists(self, path: str) -> bool: ...
    def samefile(self, path1: str, path2: str) -> bool: ...
    def realpath(self, path: str) -> str: ...
    def list(self, path: str = ".") -> list[str]: ...
    def list_detailed(self, path: str = ".") -> list[FileInfo]: ...
    def listdir(self, path: str = "/", recursive: bool = False) -> list[str]: ...
    def listdir_detailed(self, path: str = "/", recursive: bool = False) -> list[FileInfo]: ...
    def rmdir(self, path: str) -> None: ...
    def remove(self, path: str) -> None: ...
    def remove_many(self, paths: list[str]) -> None: ...
    def mkdir(self, path: str, parents: bool = False, exist_ok: bool = False) -> None: ...
    def makedirs(self, path: str, exist_ok: bool = True) -> None: ...
    def rename(self, src: str, dst: str) -> None: ...
    def stat(self, path: str) -> FileMetadata: ...
    def getsize(self, path: str) -> int: ...
    def write_many(self, files: dict[str, bytes]) -> None: ...
    def get_metadata_snapshot(self) -> dict[str, FileMetadata]: ...
```

## Patched functions

| Module | Functions |
|--------|-----------|
| `builtins` | `open` |
| `os` | `listdir`, `scandir`, `remove`, `unlink`, `mkdir`, `makedirs`, `rmdir`, `rename`, `stat`, `lstat`, `getcwd`, `chdir`, `utime`, `getenv` |
| `os.path` | `exists`, `isfile`, `isdir`, `islink`, `lexists`, `samefile`, `realpath`, `abspath`, `getsize`, `expanduser`, `expandvars` |
| `pathlib` | `Path.touch` |

## Development

```bash
uv sync --extra dev
uv run pytest
```

## License

MIT
