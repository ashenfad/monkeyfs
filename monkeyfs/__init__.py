"""monkeyfs: Transparent filesystem interception via monkey-patching."""

from .base import FileInfo, FileMetadata, FileSystem
from .context import current_fs, suspend
from .isolated import IsolatedFS
from .mount import MountFS
from .patching import patch
from .readonly import ReadOnlyFS
from .virtual import VirtualFS

__all__ = [
    "current_fs",
    "FileInfo",
    "FileMetadata",
    "FileSystem",
    "IsolatedFS",
    "MountFS",
    "patch",
    "ReadOnlyFS",
    "suspend",
    "VirtualFS",
]
