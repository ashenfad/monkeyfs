# monkeyfs 🐒

Filesystem interception via monkey-patching.

Patches `open()`, `os.listdir()`, `os.stat()`, and 30+ other stdlib functions to route through a virtual or isolated filesystem. Patches are applied lazily on first `patch()` call and are inert outside the context. Uses `contextvars` for async-safe isolation between concurrent tasks. Zero dependencies.

## Install

```bash
pip install monkeyfs
```

## Quick example

```python
from monkeyfs import VirtualFS, patch

vfs = VirtualFS({})

with patch(vfs):
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
from monkeyfs import IsolatedFS, patch

isolated = IsolatedFS(root="/tmp/sandbox")

with patch(isolated):
    with open("notes.txt", "w") as f:
        f.write("hello")          # Written to /tmp/sandbox/notes.txt

    open("/etc/passwd")           # PermissionError -- outside root
```

## ReadOnlyFS

Wraps any filesystem and blocks all write operations:

```python
from monkeyfs import VirtualFS, ReadOnlyFS, patch

vfs = VirtualFS({})
vfs.write("data.csv", b"a,b,c")

ro = ReadOnlyFS(vfs)
with patch(ro):
    print(open("data.csv").read())  # a,b,c
    open("new.txt", "w")           # PermissionError
```

## MountFS

Routes operations to different filesystems by path prefix:

```python
from monkeyfs import VirtualFS, MountFS, ReadOnlyFS, patch

base = VirtualFS({})
overlay = VirtualFS({})
overlay.write("summary.md", b"# Chapter 1")

fs = MountFS(base, {"/chapters": ReadOnlyFS(overlay)})

with patch(fs):
    # Writes go to base
    with open("app.py", "w") as f:
        f.write("print('hi')")

    # Reads from /chapters go to the overlay
    print(open("/chapters/summary.md").read())  # # Chapter 1

    # Writes to /chapters are blocked (read-only)
    open("/chapters/new.md", "w")  # PermissionError
```

## Documentation

- [API Reference](docs/api.md) -- public API, FileSystem protocol, patched functions

## Development

```bash
uv sync --extra dev
uv run pytest
```

## License

MIT
