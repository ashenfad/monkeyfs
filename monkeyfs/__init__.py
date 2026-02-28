"""monkeyfs: Transparent filesystem interception via monkey-patching."""

from .base import FileInfo, FileMetadata, FileSystem
from .context import current_fs, suspend
from .isolated import IsolatedFS
from .patching import patch
from .virtual import VirtualFS

__all__ = [
    "current_fs",
    "FileInfo",
    "FileMetadata",
    "FileSystem",
    "IsolatedFS",
    "patch",
    "suspend",
    "VirtualFS",
]
