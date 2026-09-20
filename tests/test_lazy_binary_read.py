"""A binary read must cost the bytes it asks for, not the whole file.

Every test here counts what reached the backend. That is the only thing that
distinguishes the lazy file object from the ``io.BytesIO`` it replaced -- both
answer every read correctly, and only one of them moves a megabyte to hand
back a hundred bytes.
"""

from __future__ import annotations

import io
import os

import pytest

from monkeyfs import VirtualFS, patch
from monkeyfs.virtualfile import BLOCK_SIZE, MAX_CACHED_BLOCKS, LazyBinaryFile


class CountingFS(VirtualFS):
    """A VirtualFS that records every ranged read made against it."""

    def __init__(self) -> None:
        super().__init__({})
        self.reads: list[tuple[int, int]] = []
        self.bytes_read = 0

    def read(self, path: str, offset: int = 0, size: int = -1) -> bytes:
        data = super().read(path, offset, size)
        self.reads.append((offset, size))
        self.bytes_read += len(data)
        return data

    def reset(self) -> None:
        self.reads.clear()
        self.bytes_read = 0


MEGABYTE = 1024 * 1024


def _fs_with(content: bytes, path: str = "data.bin") -> CountingFS:
    fs = CountingFS()
    fs.write(path, content)
    fs.reset()
    return fs


def _pattern(length: int) -> bytes:
    """Bytes whose value identifies their offset, so a wrong range shows."""
    return bytes(i % 251 for i in range(length))


class TestLaziness:
    """What crosses from the backend, and when."""

    def test_open_reads_nothing(self):
        fs = _fs_with(_pattern(MEGABYTE))

        handle = fs.open("data.bin", "rb")

        assert fs.reads == []
        assert handle.seekable() and handle.readable()

    def test_a_hundred_bytes_at_the_end_cost_one_block(self):
        content = _pattern(MEGABYTE)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            handle.seek(-100, os.SEEK_END)
            assert handle.read(100) == content[-100:]

        assert len(fs.reads) == 1
        assert fs.bytes_read == BLOCK_SIZE  # 65,536 of 1,048,576

    def test_the_last_block_is_short(self):
        content = _pattern(1_000_000)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            handle.seek(-10, os.SEEK_END)
            assert handle.read() == content[-10:]

        # 1,000,000 is 15 whole blocks and 16,960 bytes; only the tail crosses.
        assert len(fs.reads) == 1
        assert fs.bytes_read == 1_000_000 - 15 * BLOCK_SIZE

    def test_a_cached_block_is_not_fetched_twice(self):
        content = _pattern(MEGABYTE)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            handle.seek(1000)
            assert handle.read(10) == content[1000:1010]
            before = len(fs.reads)
            handle.seek(2000)
            assert handle.read(10) == content[2000:2010]
            handle.seek(0)
            assert handle.read(4) == content[:4]

        assert before == 1
        assert len(fs.reads) == 1

    def test_a_read_of_a_whole_block_or_more_is_one_ranged_read(self):
        content = _pattern(MEGABYTE)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            handle.seek(300_000)
            assert handle.read(200_000) == content[300_000:500_000]

        assert fs.reads == [(300_000, 200_000)]
        assert fs.bytes_read == 200_000

    def test_reading_the_whole_file_is_one_call_not_one_per_block(self):
        content = _pattern(MEGABYTE)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            assert handle.read() == content

        assert len(fs.reads) == 1

    def test_the_cache_holds_a_bounded_number_of_blocks(self):
        content = _pattern(MEGABYTE)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            for index in range(MAX_CACHED_BLOCKS + 2):
                handle.seek(index * BLOCK_SIZE)
                handle.read(4)
            fetched = len(fs.reads)
            # The first block is gone from the cache, so it is fetched again.
            handle.seek(0)
            handle.read(4)

        assert fetched == MAX_CACHED_BLOCKS + 2
        assert len(fs.reads) == fetched + 1

    def test_a_footer_probe_reads_only_the_footer(self):
        """The pyarrow access pattern: seek to the end, read the footer."""
        content = _pattern(4 * MEGABYTE)
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            handle.seek(-65536, 2)
            footer = handle.read(65536)

        assert footer == content[-65536:]
        assert fs.bytes_read == 65536  # 1.6% of the file


class TestStreamSemantics:
    """The lazy object has to behave like the BytesIO it replaced."""

    @pytest.fixture
    def handle(self):
        fs = _fs_with(b"alpha\nbeta\ngamma\n")
        with fs.open("data.bin", "rb") as handle:
            yield handle

    def test_read_to_the_end_from_the_middle(self, handle):
        handle.seek(6)
        assert handle.read(-1) == b"beta\ngamma\n"
        assert handle.tell() == 17

    def test_read_past_the_end_is_truncated(self, handle):
        assert handle.read(1000) == b"alpha\nbeta\ngamma\n"

    def test_seek_past_the_end_then_read_is_empty(self, handle):
        assert handle.seek(5000) == 5000
        assert handle.read(10) == b""
        assert handle.read() == b""
        assert handle.tell() == 5000

    def test_seek_whences(self, handle):
        assert handle.seek(3, os.SEEK_SET) == 3
        assert handle.seek(2, os.SEEK_CUR) == 5
        assert handle.seek(-6, os.SEEK_END) == 11
        assert handle.read(5) == b"gamma"

    def test_a_negative_seek_is_refused(self, handle):
        with pytest.raises(ValueError):
            handle.seek(-1)
        with pytest.raises(ValueError):
            handle.seek(-100, os.SEEK_END)

    def test_an_unknown_whence_is_refused(self, handle):
        with pytest.raises(ValueError):
            handle.seek(0, 7)

    def test_readinto_fills_what_it_can(self, handle):
        buffer = bytearray(5)
        assert handle.readinto(buffer) == 5
        assert bytes(buffer) == b"alpha"

        handle.seek(-3, os.SEEK_END)
        partial = bytearray(10)
        assert handle.readinto(partial) == 3
        assert bytes(partial[:3]) == b"ma\n"

        assert handle.readinto(bytearray(4)) == 0

    def test_readinto_takes_a_memoryview(self, handle):
        buffer = bytearray(5)
        assert handle.readinto(memoryview(buffer)) == 5
        assert bytes(buffer) == b"alpha"

    def test_readline_and_iteration(self, handle):
        assert handle.readline() == b"alpha\n"
        assert handle.readlines() == [b"beta\n", b"gamma\n"]
        handle.seek(0)
        assert list(handle) == [b"alpha\n", b"beta\n", b"gamma\n"]
        assert handle.readline() == b""

    def test_readline_honours_a_size_limit(self, handle):
        assert handle.readline(3) == b"alp"
        assert handle.readline() == b"ha\n"

    def test_a_line_across_a_block_boundary_is_whole(self):
        # One line straddling three blocks, with a short line either side.
        content = b"first\n" + b"x" * (2 * BLOCK_SIZE) + b"\nlast\n"
        fs = _fs_with(content)

        with fs.open("data.bin", "rb") as handle:
            assert handle.readline() == b"first\n"
            assert handle.readline() == b"x" * (2 * BLOCK_SIZE) + b"\n"
            assert handle.readline() == b"last\n"
            assert handle.readline() == b""

    def test_capabilities(self, handle):
        assert handle.readable() is True
        assert handle.seekable() is True
        assert handle.writable() is False
        with pytest.raises(io.UnsupportedOperation):
            handle.write(b"no")

    def test_fileno_is_unsupported_rather_than_wrong(self, handle):
        """Readers probe for a descriptor and accept not having one."""
        with pytest.raises(io.UnsupportedOperation):
            handle.fileno()

    def test_closed_and_context_manager(self):
        fs = _fs_with(b"payload")
        handle = fs.open("data.bin", "rb")
        assert handle.closed is False

        with handle:
            assert handle.read(3) == b"pay"

        assert handle.closed is True
        with pytest.raises(ValueError, match="closed file"):
            handle.read(1)
        with pytest.raises(ValueError, match="closed file"):
            handle.seek(0)
        with pytest.raises(ValueError, match="closed file"):
            handle.tell()

    def test_an_empty_file_reads_empty_without_touching_the_backend(self):
        fs = _fs_with(b"")

        with fs.open("data.bin", "rb") as handle:
            assert handle.read() == b""
            assert handle.readline() == b""

        assert fs.reads == []

    def test_a_missing_file_still_raises(self):
        fs = CountingFS()
        with pytest.raises(FileNotFoundError):
            fs.open("nope.bin", "rb")

    def test_text_mode_and_write_modes_still_materialize(self):
        """The narrow claim: only binary reads went lazy."""
        fs = _fs_with(b"alpha\nbeta\n")

        with fs.open("data.bin", "r") as handle:
            assert handle.read() == "alpha\nbeta\n"
        assert not isinstance(fs.open("data.bin", "rb+"), LazyBinaryFile)
        assert not isinstance(fs.open("data.bin", "ab"), LazyBinaryFile)


class TestThroughPatchedOpen:
    """The whole point is that builtins.open() is the lazy one."""

    def test_builtin_open_serves_a_range(self):
        content = _pattern(MEGABYTE)
        fs = _fs_with(content)

        with patch(fs):
            with open("data.bin", "rb") as handle:
                handle.seek(-100, os.SEEK_END)
                tail = handle.read(100)

        assert tail == content[-100:]
        assert fs.bytes_read == BLOCK_SIZE

    def test_a_whole_read_through_the_builtin_is_still_whole(self):
        content = _pattern(3000)
        fs = _fs_with(content)

        with patch(fs):
            with open("data.bin", "rb") as handle:
                assert handle.read() == content
