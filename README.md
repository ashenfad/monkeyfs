# monkeyfs 🐒

Transparent filesystem interception via monkey-patching.

Patches `open()`, `os.listdir()`, `os.stat()`, and 30+ other stdlib functions to route through a virtual or isolated filesystem. Patches are installed at import time and are inert until activated with `patch()`. Uses `contextvars` for async-safe isolation between concurrent tasks. Zero dependencies.

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

isolated = IsolatedFS(root="/tmp/sandbox", state={})

with patch(isolated):
    with open("notes.txt", "w") as f:
        f.write("hello")          # Written to /tmp/sandbox/notes.txt

    open("/etc/passwd")           # PermissionError -- outside root
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
