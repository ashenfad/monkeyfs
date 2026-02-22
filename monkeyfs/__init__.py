"""monkeyfs: Transparent filesystem interception via monkey-patching."""

from .base import FileInfo, FileMetadata, FileSystem
from .config import FSConfig, IsolatedFSConfig, VirtualFSConfig, connect_fs
from .context import suspend_fs_interception
from .isolated import IsolatedFS
from .patching import get_current_fs, install, use_fs
from .virtual import VirtualFile, VirtualFS

__all__ = [
    "connect_fs",
    "FileInfo",
    "FileMetadata",
    "FileSystem",
    "FSConfig",
    "get_current_fs",
    "install",
    "IsolatedFS",
    "IsolatedFSConfig",
    "suspend_fs_interception",
    "use_fs",
    "VirtualFile",
    "VirtualFS",
    "VirtualFSConfig",
]
