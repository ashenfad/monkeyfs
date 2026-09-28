"""Tests for VirtualFS bulk operations (write_many, remove_many)."""

import pytest

from monkeyfs import VirtualFS


class TestBulkOperations:
    """Test VirtualFS bulk operations."""

    def test_write_many_basic(self):
        """Test writing multiple files at once."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content 1",
            "file2.txt": b"content 2",
            "dir/file3.txt": b"content 3",
        }

        vfs.write_many(files)

        # All files should exist
        assert vfs.read("file1.txt") == b"content 1"
        assert vfs.read("file2.txt") == b"content 2"
        assert vfs.read("dir/file3.txt") == b"content 3"

    def test_write_many_validates_all_bytes(self):
        """Test that write_many validates all content is bytes."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content 1",
            "file2.txt": "not bytes",  # Invalid!
        }

        with pytest.raises(TypeError, match="Expected bytes for 'file2.txt'"):
            vfs.write_many(files)

        # No files should be written (validation happens first)
        assert not vfs.exists("file1.txt")

    def test_write_many_with_dict_state(self):
        """Test that write_many works with dict state (no snapshot method)."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content 1",
            "file2.txt": b"content 2",
        }

        # Should not raise - dict doesn't have snapshot()
        vfs.write_many(files)

        assert vfs.exists("file1.txt")
        assert vfs.exists("file2.txt")

    def test_remove_many_basic(self):
        """Test removing multiple files at once."""
        vfs = VirtualFS({})

        # Create files
        vfs.write("file1.txt", b"content 1")
        vfs.write("file2.txt", b"content 2")
        vfs.write("file3.txt", b"content 3")

        # Remove two of them
        vfs.remove_many(["file1.txt", "file2.txt"])

        # Removed files should not exist
        assert not vfs.exists("file1.txt")
        assert not vfs.exists("file2.txt")
        # Remaining file should still exist
        assert vfs.exists("file3.txt")

    def test_remove_many_missing_file_removes_preceding(self):
        """Test that remove_many removes files up to the missing one."""
        vfs = VirtualFS({})

        vfs.write("file1.txt", b"content 1")

        with pytest.raises(FileNotFoundError):
            vfs.remove_many(["file1.txt", "file2.txt"])

        # file1.txt was removed before file2.txt failed
        assert not vfs.exists("file1.txt")

    def test_remove_many_empty_list(self):
        """Test that removing empty list works."""
        vfs = VirtualFS({})

        # Should not raise
        vfs.remove_many([])


class BatchingState(dict):
    """A dict that also answers ``get_many``, counting how it is read."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.gets: list[str] = []
        self.batches: list[tuple[str, ...]] = []

    def get(self, key, default=None):
        self.gets.append(key)
        return super().get(key, default)

    def get_many(self, *keys):
        self.batches.append(keys)
        return {k: dict.__getitem__(self, k) for k in keys if k in self}


class TestReadMany:
    def test_reads_every_file_in_one_call(self):
        state = BatchingState()
        vfs = VirtualFS(state)
        for i in range(20):
            vfs.write(f"/d{i % 2}/f{i}.txt", f"file {i}".encode())
        state.gets.clear()
        state.batches.clear()

        found = vfs.read_many([f"/d{i % 2}/f{i}.txt" for i in range(20)])

        assert found == {f"/d{i % 2}/f{i}.txt": f"file {i}".encode() for i in range(20)}
        assert len(state.batches) == 1
        assert state.gets == []

    def test_leaves_out_what_is_not_a_file(self):
        vfs = VirtualFS({})
        vfs.write("/d/a.txt", b"a")

        assert vfs.read_many(["/d/a.txt", "/d", "/nope.txt"]) == {"/d/a.txt": b"a"}

    def test_answers_under_the_paths_as_given(self):
        vfs = VirtualFS({})
        vfs.write("/d/a.txt", b"a")
        vfs.chdir("/d")

        assert vfs.read_many(["a.txt", "/d/a.txt"]) == {
            "a.txt": b"a",
            "/d/a.txt": b"a",
        }

    def test_matches_read_without_get_many(self):
        vfs = VirtualFS({})
        vfs.write_many({"x.txt": b"x", "y/z.txt": b"z"})

        assert vfs.read_many(["x.txt", "y/z.txt"]) == {
            "x.txt": vfs.read("x.txt"),
            "y/z.txt": vfs.read("y/z.txt"),
        }

    def test_empty(self):
        assert VirtualFS({}).read_many([]) == {}

    def test_isolated(self, tmp_path):
        from monkeyfs import IsolatedFS

        fs = IsolatedFS(str(tmp_path))
        fs.write("a.txt", b"a")
        fs.mkdir("d")

        assert fs.read_many(["a.txt", "d", "missing.txt"]) == {"a.txt": b"a"}

    def test_mount_routes_each_path_to_its_filesystem(self):
        from monkeyfs import MountFS

        base_state, mounted_state = BatchingState(), BatchingState()
        base, mounted = VirtualFS(base_state), VirtualFS(mounted_state)
        base.write("/top.txt", b"top")
        mounted.write("/inner.txt", b"inner")
        fs = MountFS(base, {"/m": mounted})
        base_state.batches.clear()
        mounted_state.batches.clear()

        assert fs.read_many(["/top.txt", "/m/inner.txt", "/m/gone.txt"]) == {
            "/top.txt": b"top",
            "/m/inner.txt": b"inner",
        }
        assert len(base_state.batches) == 1
        assert len(mounted_state.batches) == 1

    def test_mount_reads_a_filesystem_without_it_file_by_file(self):
        from monkeyfs import MountFS

        class Plain:
            """A mounted backend with ``read`` and no ``read_many``."""

            def __init__(self, files):
                self.files = files

            def read(self, path):
                try:
                    return self.files[path]
                except KeyError:
                    raise FileNotFoundError(path) from None

        fs = MountFS(VirtualFS({}), {"/p": Plain({"/x.txt": b"x"})})

        assert fs.read_many(["/p/x.txt", "/p/y.txt"]) == {"/p/x.txt": b"x"}

    def test_read_only_forwards_it(self):
        from monkeyfs import ReadOnlyFS

        vfs = VirtualFS({})
        vfs.write("a.txt", b"a")

        assert ReadOnlyFS(vfs).read_many(["a.txt"]) == {"a.txt": b"a"}
