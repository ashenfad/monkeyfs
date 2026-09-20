"""The ranged read, across every backend and wrapper.

``read(path, offset=0, size=-1)`` has one set of semantics, and a caller must
be able to rely on them without knowing which filesystem answered. These tests
run the same expectations against each one, so a backend that quietly ignores
the range -- or clamps a negative offset instead of refusing it -- fails here
rather than in whatever is reading a parquet footer.
"""

from __future__ import annotations

import pytest

from monkeyfs import IsolatedFS, MountFS, ReadOnlyFS, VirtualFS

CONTENT = b"0123456789abcdef"


def _virtual() -> VirtualFS:
    fs = VirtualFS({})
    fs.write("data.bin", CONTENT)
    return fs


def _isolated(tmp_path) -> IsolatedFS:
    fs = IsolatedFS(root=str(tmp_path))
    fs.write("data.bin", CONTENT)
    return fs


def _mount_base(tmp_path) -> MountFS:
    base = VirtualFS({})
    base.write("data.bin", CONTENT)
    return MountFS(base)


def _mount_point(tmp_path) -> MountFS:
    inner = VirtualFS({})
    inner.write("data.bin", CONTENT)
    return MountFS(VirtualFS({}), {"/mnt": inner})


def _readonly(tmp_path) -> ReadOnlyFS:
    return ReadOnlyFS(_virtual())


#: Each entry builds a filesystem holding ``CONTENT`` at the given path.
BACKENDS = [
    ("VirtualFS", lambda tmp: _virtual(), "data.bin"),
    ("IsolatedFS", _isolated, "data.bin"),
    ("MountFS-base", _mount_base, "/data.bin"),
    ("MountFS-mount", _mount_point, "/mnt/data.bin"),
    ("ReadOnlyFS", _readonly, "data.bin"),
]


@pytest.fixture(params=BACKENDS, ids=[name for name, _, _ in BACKENDS])
def backend(request, tmp_path):
    _, build, path = request.param
    return build(tmp_path), path


class TestRangedReadSemantics:
    """One contract, whoever implements it."""

    def test_defaults_are_the_whole_file(self, backend):
        fs, path = backend
        assert fs.read(path) == CONTENT

    def test_offset_with_no_size_reads_to_the_end(self, backend):
        fs, path = backend
        assert fs.read(path, 10) == CONTENT[10:]

    def test_offset_and_size_read_that_range(self, backend):
        fs, path = backend
        assert fs.read(path, 4, 6) == CONTENT[4:10]

    def test_size_of_zero_reads_nothing(self, backend):
        fs, path = backend
        assert fs.read(path, 4, 0) == b""

    def test_a_range_past_the_end_is_truncated(self, backend):
        fs, path = backend
        assert fs.read(path, 12, 1000) == CONTENT[12:]

    def test_a_read_at_the_end_is_empty(self, backend):
        fs, path = backend
        assert fs.read(path, len(CONTENT)) == b""

    def test_a_read_past_the_end_is_empty(self, backend):
        fs, path = backend
        assert fs.read(path, len(CONTENT) + 100) == b""
        assert fs.read(path, len(CONTENT) + 100, 10) == b""

    def test_a_negative_offset_is_refused(self, backend):
        fs, path = backend
        with pytest.raises(ValueError):
            fs.read(path, -1)

    def test_a_missing_file_still_raises(self, backend):
        fs, path = backend
        with pytest.raises(FileNotFoundError):
            fs.read("nope.bin", 0, 4)

    def test_arguments_may_be_named(self, backend):
        fs, path = backend
        assert fs.read(path, offset=2, size=3) == CONTENT[2:5]


class TestIsolatedFSReadsOnlyTheRange:
    """The host backend must seek, not slurp."""

    def test_a_range_does_not_read_the_whole_file(self, tmp_path):
        fs = IsolatedFS(root=str(tmp_path))
        fs.write("big.bin", b"\0" * 1_000_000 + b"tail")

        assert fs.read("big.bin", 1_000_000) == b"tail"

    def test_the_last_bytes_come_back_without_the_first(self, tmp_path):
        fs = IsolatedFS(root=str(tmp_path))
        fs.write("big.bin", bytes(range(256)) * 4_000)

        assert fs.read("big.bin", 1_023_999, 1) == bytes([255])
