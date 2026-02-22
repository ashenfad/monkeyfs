"""monkeyfs: Transparent filesystem interception via monkey-patching."""

from .base import FileInfo, FileMetadata, FileSystem
from .config import FSConfig, IsolatedFSConfig, VirtualFSConfig, connect_fs
from .context import current_fs, suspend
from .isolated import IsolatedFS
from .patching import patch
from .virtual import VirtualFile, VirtualFS

__all__ = [
    "connect_fs",
    "current_fs",
    "FileInfo",
    "FileMetadata",
    "FileSystem",
    "FSConfig",
    "IsolatedFS",
    "IsolatedFSConfig",
    "patch",
    "suspend",
    "VirtualFile",
    "VirtualFS",
    "VirtualFSConfig",
]
