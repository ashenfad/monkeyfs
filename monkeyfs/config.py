"""Configuration for filesystem access.

Provides configuration dataclasses and connect_fs factory function for
configuring filesystem access (virtual or isolated).
"""

from dataclasses import dataclass
from typing import Literal


@dataclass
class VirtualFSConfig:
    """Configuration for virtual (in-memory) filesystem.

    Attributes:
        type: Always "virtual".
        max_size_mb: Maximum total size of all files in megabytes.
            None means unlimited.
    """

    type: Literal["virtual"] = "virtual"
    max_size_mb: int | None = None


@dataclass
class IsolatedFSConfig:
    """Configuration for isolated (real) filesystem with path restriction.

    Attributes:
        type: Always "isolated".
        root: Absolute path to root directory (all file operations restricted to this path).
        tracking: Whether to track file changes and emit events (default: False).
        per_session: Whether to create session subdirectories (default: False).
    """

    type: Literal["isolated"] = "isolated"
    root: str = ""
    tracking: bool = False
    per_session: bool = False


# Type alias for all filesystem configs
FSConfig = VirtualFSConfig | IsolatedFSConfig


def connect_fs(
    type: Literal["virtual", "isolated"] = "virtual",
    **kwargs,
) -> FSConfig:
    """Configure filesystem access.

    Creates a filesystem configuration.

    Args:
        type: FileSystem type.
            - "virtual": In-memory filesystem backed by a mapping.
                        Files persist with state and participate in versioning.
            - "isolated": Real filesystem restricted to a directory.
                         Requires 'root' argument.
        **kwargs: Additional configuration for the filesystem type.
            For type="isolated":
                - root (str): Required. Absolute path to root directory.
                - tracking (bool): Optional. Track file changes (default: False).

    Returns:
        FSConfig for initialization.

    Examples:
        Virtual filesystem:
        >>> connect_fs(type="virtual")
        VirtualFSConfig(type='virtual', max_size_mb=None)

        Isolated filesystem:
        >>> connect_fs(type="isolated", root="/path/to/project", tracking=True)
        IsolatedFSConfig(type='isolated', root='/path/to/project', tracking=True, per_session=False)
    """
    if type == "virtual":
        max_size_mb = kwargs.pop("max_size_mb", None)
        if kwargs:
            raise ValueError(
                f"Unexpected arguments for virtual fs: {list(kwargs.keys())}"
            )
        return VirtualFSConfig(type=type, max_size_mb=max_size_mb)

    elif type == "isolated":
        # Extract isolated-specific kwargs
        root = kwargs.pop("root", "")
        tracking = kwargs.pop("tracking", False)
        per_session = kwargs.pop("per_session", False)

        if kwargs:
            raise ValueError(
                f"Unexpected arguments for isolated fs: {list(kwargs.keys())}"
            )

        if not root:
            raise ValueError("Isolated filesystem requires 'root' parameter")

        return IsolatedFSConfig(root=root, tracking=tracking, per_session=per_session)

    else:
        raise ValueError(
            f"Unsupported filesystem type: {type}. Use 'virtual' or 'isolated'."
        )
