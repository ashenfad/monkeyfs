"""FileSystem patching infrastructure.

Provides context-aware patching of Python's filesystem operations (builtins.open,
os.listdir, etc.) to route through any FileSystem Protocol implementation when
active. Uses contextvars for async-safe isolation between concurrent tasks.

Patches are applied lazily on first ``patch()`` call, not at import time.
Each patched function checks the context variable to determine whether to
use the active filesystem or the real filesystem.
"""

from .install import get_current_fs, install, patch

__all__ = ["get_current_fs", "install", "patch"]
