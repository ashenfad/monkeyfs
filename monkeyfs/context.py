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

# Internal flag controlling whether VFS defers commits to the backing store.
_defer_commits: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "monkeyfs_defer_commits", default=False
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


@contextmanager
def defer_commits() -> Iterator[None]:
    """Suppress per-mutation commits to the VirtualFS backing store.

    VirtualFS accepts any ``MutableMapping[str, bytes]`` as its state.
    If the mapping also has a ``commit()`` method (e.g. gitkv, shelve,
    DiskCache), VirtualFS calls it after each mutation so changes are
    persisted immediately.

    Inside this context manager those automatic commits are suppressed,
    letting you batch many writes and commit once at the end. This is
    useful during bulk operations (agent execution, imports, etc.) where
    per-write persistence would be expensive or cause recursion with
    disk-backed state.

    Example::

        with defer_commits():
            with patch(vfs):
                # Many writes happen here — no commit() calls
                ...
        # After exiting, call state.commit() yourself if needed
    """
    token = _defer_commits.set(True)
    try:
        yield
    finally:
        _defer_commits.reset(token)
