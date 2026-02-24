"""FileSystem patching infrastructure.

Provides context-aware patching of Python's filesystem operations (builtins.open,
os.listdir, etc.) to route through any FileSystem Protocol implementation when
active. Uses contextvars for async-safe isolation between concurrent tasks.

The patching is applied once at module import. Each patched function checks
the context variable to determine whether to use the active filesystem or
the real filesystem.
"""

from .install import get_current_fs, install, patch

__all__ = ["get_current_fs", "install", "patch"]

# Install patches at import time. They are inert when no filesystem
# is active (current_fs is None) and activate when patch() sets one.
install()
