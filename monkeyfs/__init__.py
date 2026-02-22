"""monkeyfs: Transparent filesystem interception via monkey-patching."""

from .base import FileInfo, FileMetadata, FileSystem
from .config import FSConfig, IsolatedFSConfig, VirtualFSConfig, connect_fs
from .context import current_fs, defer_commits, suspend
from .isolated import IsolatedFS
from .patching import get_current_fs, install, patch
from .virtual import VirtualFile, VirtualFS

__all__ = [
    "current_fs",
    "connect_fs",
    "FileInfo",
    "FileMetadata",
    "FileSystem",
    "FSConfig",
    "get_current_fs",
    "install",
    "IsolatedFS",
    "IsolatedFSConfig",
    "patch",
    "defer_commits",
    "suspend",
    "VirtualFile",
    "VirtualFS",
    "VirtualFSConfig",
]
