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


@contextmanager
def suspend() -> Iterator[None]:
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
