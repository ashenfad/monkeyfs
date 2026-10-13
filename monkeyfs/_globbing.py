"""Glob matching by path segment, over what a filesystem's ``list()`` shows.

One matcher for every backend that has no host directory to hand to
``pathlib``: ``VirtualFS.glob`` uses it, and so does ``MountFS`` for a
mount without a ``glob`` of its own. The semantics are ``pathlib``'s, as
``IsolatedFS`` answers on Python 3.13 and later, so the same tree gives
the same answer whichever of them holds it.
"""

from __future__ import annotations

import fnmatch
from typing import Any

WILDCARDS = frozenset("*?[")


def glob_from_list(fs: Any, pattern: str) -> list[str]:
    """Match an absolute ``pattern`` against what ``fs.list()`` reports.

    ``*``, ``?`` and ``[...]`` match within one path segment, a ``**``
    segment matches zero or more directories (a trailing one matches
    everything below, the directory itself included), hidden names match
    like any other, and directories match as well as files. Results are
    absolute and sorted.

    Only the pattern's fixed leading directory -- everything before the
    first segment with a wildcard -- is listed, recursively, so
    ``/data/2024/*.csv`` lists ``/data/2024`` and nothing else.
    """
    # Empty and "." segments name nothing, as in pathlib.
    parts = [part for part in pattern.split("/") if part and part != "."]
    fixed: list[str] = []
    for part in parts:
        if WILDCARDS.intersection(part):
            break
        fixed.append(part)
    root = "/" + "/".join(fixed)
    if len(fixed) == len(parts):
        return [root] if fs.exists(root) else []
    if not fs.isdir(root):
        return []

    rest = parts[len(fixed) :]
    base = root.rstrip("/")
    found = [
        f"{base}/{entry}"
        for entry in fs.list(root, recursive=True)
        if segments_match(rest, entry.split("/"))
    ]
    # A pattern of only ``**`` below the fixed directory matches that
    # directory too, as pathlib's does -- unless it is the root, which
    # has no name to report.
    if root != "/" and segments_match(rest, []):
        found.append(root)
    return sorted(found)


def segments_match(pattern: list[str], parts: list[str]) -> bool:
    """Whether path segments ``parts`` match pattern segments ``pattern``."""
    if not pattern:
        return not parts
    head, tail = pattern[0], pattern[1:]
    if head == "**":
        while tail and tail[0] == "**":
            tail = tail[1:]
        return any(segments_match(tail, parts[i:]) for i in range(len(parts) + 1))
    return (
        bool(parts)
        and fnmatch.fnmatchcase(parts[0], head)
        and segments_match(tail, parts[1:])
    )
