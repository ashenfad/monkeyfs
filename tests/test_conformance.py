"""The conformance kit, run against every backend and against broken ones.

A kit that passes everything is worth nothing, so half of these tests are
backends that are wrong on purpose -- a read that ignores its range, one that
clamps a negative offset, a filesystem missing a required method -- and assert
that the kit says so.
"""

from __future__ import annotations

import pytest

from monkeyfs import IsolatedFS, MountFS, VirtualFS, check_filesystem
from monkeyfs.conformance import SCRATCH

from .test_open_optional import TermishFS


class TestConformingBackends:
    """Every backend in the tree passes its own kit."""

    def test_virtual(self):
        check_filesystem(VirtualFS({}))

    def test_isolated(self, tmp_path):
        check_filesystem(IsolatedFS(root=str(tmp_path)))

    def test_mounted(self):
        check_filesystem(MountFS(VirtualFS({})))

    def test_a_backend_with_no_open_of_its_own(self):
        """The synthesized open() is what the kit checks for this one."""
        check_filesystem(TermishFS())

    def test_the_kit_cleans_up_after_itself(self):
        fs = VirtualFS({})
        check_filesystem(fs)
        assert fs.list("/") == []
        assert not fs.exists(SCRATCH)


class TestBackendsThatShouldFail:
    """What the kit is for."""

    def test_a_read_that_ignores_its_range(self):
        class WholeFileOnly(VirtualFS):
            def read(self, path, offset=0, size=-1):
                return super().read(path)

        with pytest.raises(AssertionError, match="read"):
            check_filesystem(WholeFileOnly({}))

    def test_a_read_that_refuses_the_arguments(self):
        class UnrangedRead(VirtualFS):
            def read(self, path):  # the old signature
                return super().read(path)

        with pytest.raises(AssertionError, match="offset and size"):
            check_filesystem(UnrangedRead({}))

    def test_a_read_that_clamps_a_negative_offset(self):
        class Clamping(VirtualFS):
            def read(self, path, offset=0, size=-1):
                return super().read(path, max(offset, 0), size)

        with pytest.raises(AssertionError, match="ValueError"):
            check_filesystem(Clamping({}))

    def test_a_read_that_pads_past_the_end(self):
        class Padding(VirtualFS):
            def read(self, path, offset=0, size=-1):
                data = super().read(path, offset, size)
                if size >= 0 and len(data) < size:
                    data += b"\0" * (size - len(data))
                return data

        with pytest.raises(AssertionError, match="truncated"):
            check_filesystem(Padding({}))

    def test_a_missing_required_method(self):
        class NoStat(VirtualFS):
            stat = None

        with pytest.raises(AssertionError, match="stat"):
            check_filesystem(NoStat({}))

    def test_a_stat_that_lies_about_size(self):
        class WrongSize(VirtualFS):
            def stat(self, path):
                meta = super().stat(path)
                meta.size = max(meta.size - 1, 0)
                return meta

        with pytest.raises(AssertionError, match="size"):
            check_filesystem(WrongSize({}))

    def test_a_remove_that_forgives_a_missing_path(self):
        class ForgivingRemove(VirtualFS):
            def remove(self, path):
                try:
                    super().remove(path)
                except FileNotFoundError:
                    pass

        with pytest.raises(AssertionError, match="FileNotFoundError"):
            check_filesystem(ForgivingRemove({}))

    def test_a_filesystem_that_is_not_empty(self):
        fs = VirtualFS({})
        fs.makedirs(SCRATCH)

        with pytest.raises(AssertionError, match="empty filesystem"):
            check_filesystem(fs)
