"""Read-only filesystem wrapper.

Wraps any FileSystem and enforces read-only access with an *allowlist*: the
operations classified as read-only in ``monkeyfs.base`` are forwarded, and
everything else -- including attributes this module has never heard of -- is
refused.
"""

from __future__ import annotations

import os
from typing import Any, Callable

from .base import READ_METHODS, WRITE_METHODS

# The read/write split lives in ``base.py``, beside the protocol it classifies,
# and both sets are re-exported here under the names ``docs/api.md`` and
# callers already use.
#
# It used to be a second list maintained here by hand, cross-checked against
# the protocol by reading both. That is the arrangement that let ``utime``
# reach a backend the wrapper had never classified. Now the patch layer's
# dispatch surface, this allowlist and ``MountFS``'s forwarding all come from
# the same frozensets, so a method added to the protocol reaches all three.
#
# ``chdir`` is a read: it moves the filesystem's own working directory and
# stores nothing, and it is a required protocol method, so refusing it would
# take out ``os.chdir()`` under every read-only mount. ``open`` and ``access``
# are in the read set but are implemented below rather than forwarded, because
# both are read-only only for some arguments.
__all__ = ["READ_METHODS", "WRITE_METHODS", "ReadOnlyFS"]


class ReadOnlyFS:
    """Wraps any FileSystem and refuses everything that is not a known read.

    Read operations delegate to the wrapped filesystem. Write operations, and
    anything not classified as one or the other, raise ``PermissionError``.

    The direction matters. An earlier version listed the *mutating* methods and
    forwarded the rest, so any mutating method it did not name went straight
    through -- and a backend gaining a method silently widened the wrapper's
    guarantee. Enumerating the read-only surface instead means an unclassified
    method fails closed: refused, with an error saying how to classify it.

    Example:
        >>> from monkeyfs import VirtualFS, ReadOnlyFS
        >>> vfs = VirtualFS({})
        >>> vfs.write("data.csv", b"a,b,c")
        >>> ro = ReadOnlyFS(vfs)
        >>> ro.read("data.csv")
        b'a,b,c'
        >>> ro.write("x.txt", b"hi")  # raises PermissionError
    """

    def __init__(self, fs: Any):
        self._fs = fs

    def __getattr__(self, name: str) -> Any:
        # Private and dunder names are never forwarded. The interpreter and the
        # stdlib probe these on arbitrary objects (``copy``, ``pickle``,
        # ``inspect``) and expect AttributeError, and a backend's private
        # attributes are its internals, not part of any read surface. This also
        # breaks the infinite recursion an instance built without __init__ --
        # by ``copy.copy()``, say -- would otherwise hit looking up ``_fs``.
        if name.startswith("_"):
            raise AttributeError(
                f"{type(self).__name__!r} object has no attribute {name!r}"
            )

        if name in WRITE_METHODS:
            return self._refuse_write(name)

        if name in READ_METHODS:
            # AttributeError from a backend that lacks an optional method is
            # left to propagate: the patch layer probes with
            # ``getattr(fs, "islink", None)`` and needs that answer.
            return getattr(self._fs, name)

        raise PermissionError(
            f"Read-only filesystem: {name!r} is not on ReadOnlyFS's read-only "
            f"allowlist, so it is refused rather than forwarded to "
            f"{type(self._fs).__name__}. ReadOnlyFS enumerates the operations "
            f"it knows to be safe and refuses everything else, so a method "
            f"added to a backend cannot widen the wrapper's guarantee by "
            f"accident. If {name} only observes the filesystem, add it to one "
            f"of the read sets in monkeyfs.base; if it changes stored state, "
            f"add it to one of the write sets there."
        )

    @staticmethod
    def _deny() -> None:
        raise PermissionError("Read-only filesystem")

    @staticmethod
    def _refuse_write(name: str) -> Callable[..., Any]:
        """A stand-in for a mutating method that raises when called.

        Refusing at call time rather than at attribute lookup keeps
        ``hasattr(ro, "write")`` truthful -- the filesystem does have the
        operation, it is the wrapper that will not perform it -- so capability
        probes still work and get the informative failure when they follow
        through.
        """

        def denied(*args: Any, **kwargs: Any) -> Any:
            raise PermissionError(
                f"Read-only filesystem: {name}() modifies the filesystem"
            )

        denied.__name__ = name
        return denied

    # -- Mode-sensitive operations --

    def open(self, path: str, mode: str = "r", **kwargs: Any) -> Any:
        """Open ``path``; read modes only.

        ``w``, ``a`` and ``x`` create, truncate or extend, and ``+`` turns any
        mode -- ``r+`` included -- into an update mode that can write in place.
        Everything left (``r``, ``rb``, ``rt``, and encoding/newline kwargs) is
        a pure read.
        """
        if any(c in mode for c in "wax+"):
            self._deny()
        return self._fs.open(path, mode, **kwargs)

    def access(self, path: str, mode: int) -> bool:
        """Answer ``os.access()``; write access is always denied."""
        if mode & os.W_OK:
            return False
        return self._fs.access(path, mode)
