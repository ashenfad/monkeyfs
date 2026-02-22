"""Context variables for filesystem isolation.

Shared context variables used by patching.py and filesystem implementations
to coordinate filesystem routing and prevent recursion loops.
"""

import contextvars
from contextlib import contextmanager
from typing import Any, Iterator

# Context variable holding the current filesystem
current_fs: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "monkeyfs_current_fs", default=None
)

# Control whether VFS should defer snapshots
# When True, VirtualFile.close() will not trigger snapshots
# When False (default), VirtualFile.close() will snapshot normally
vfs_defer_snapshots: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "monkeyfs_vfs_defer_snapshots", default=False
)


@contextmanager
def suspend_fs_interception() -> Iterator[None]:
    """Temporarily disable filesystem interception in the current context.

    Use this when implementing internal filesystem operations (like inside
    IsolatedFS) that need to perform real I/O without triggering the
    patched functions recursively.
    """
    token = current_fs.set(None)
    try:
        yield
    finally:
        current_fs.reset(token)
