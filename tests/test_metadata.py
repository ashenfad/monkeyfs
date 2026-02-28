"""Tests for VirtualFS file metadata tracking."""

from monkeyfs import VirtualFS


class TestFileMetadata:
    """Test file metadata tracking (size, timestamps)."""

    def test_write_creates_metadata(self):
        """Test that writing a file creates metadata."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"hello world")

        meta = vfs.stat("file.txt")
        assert meta.size == 11
        assert meta.created_at  # Has timestamp
        assert meta.modified_at  # Has timestamp
        assert meta.created_at == meta.modified_at  # Same for new file

    def test_modify_file_updates_metadata(self):
        """Test that modifying a file updates modified_at but preserves created_at."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"hello")
        original_meta = vfs.stat("file.txt")

        # Modify file
        vfs.write("file.txt", b"hello world again")
        new_meta = vfs.stat("file.txt")

        assert new_meta.size == 17
        assert new_meta.created_at == original_meta.created_at  # Preserved
        assert new_meta.modified_at >= original_meta.modified_at  # Updated

    def test_rename_preserves_created_at(self):
        """Test that renaming preserves created_at timestamp."""
        vfs = VirtualFS({})

        vfs.write("old.txt", b"content")
        original_meta = vfs.stat("old.txt")

        vfs.rename("old.txt", "new.txt")
        new_meta = vfs.stat("new.txt")

        assert new_meta.created_at == original_meta.created_at  # Preserved
        assert new_meta.size == original_meta.size

    def test_remove_deletes_metadata(self):
        """Test that removing a file deletes its metadata."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"content")
        assert vfs.stat("file.txt")  # Metadata exists

        vfs.remove("file.txt")

        # File and metadata gone
        assert not vfs.exists("file.txt")

    def test_stat_fails_for_nonexistent_file(self):
        """Test that stat() raises FileNotFoundError for missing files."""
        vfs = VirtualFS({})

        try:
            vfs.stat("nonexistent.txt")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass

    def test_stat_file_without_metadata(self):
        """Test that stat() returns synthetic metadata for files with no metadata entry."""
        state = {}
        vfs = VirtualFS(state)

        # Insert a file directly into the backing dict, bypassing write()
        from monkeyfs.virtual import VirtualFS as _VFS

        key = _VFS._encode_path(vfs, "raw.txt")
        state[key] = b"hello"

        assert vfs.exists("raw.txt")
        meta = vfs.stat("raw.txt")
        assert meta.size == 0
        assert meta.is_dir is False

    def test_write_many_creates_metadata_for_all(self):
        """Test that write_many creates metadata for all files."""
        vfs = VirtualFS({})

        files = {
            "file1.txt": b"content1",
            "file2.txt": b"content2",
            "dir/file3.txt": b"content3",
        }

        vfs.write_many(files)

        # All files have metadata
        meta1 = vfs.stat("file1.txt")
        assert meta1.size == 8

        meta2 = vfs.stat("file2.txt")
        assert meta2.size == 8

        meta3 = vfs.stat("dir/file3.txt")
        assert meta3.size == 8

    def test_remove_many_deletes_all_metadata(self):
        """Test that remove_many deletes metadata for all files."""
        vfs = VirtualFS({})

        vfs.write_many({"file1.txt": b"a", "file2.txt": b"b"})
        assert vfs.stat("file1.txt")
        assert vfs.stat("file2.txt")

        vfs.remove_many(["file1.txt", "file2.txt"])

        assert not vfs.exists("file1.txt")
        assert not vfs.exists("file2.txt")

    def test_list_detailed_returns_file_info(self):
        """Test that list_detailed returns FileInfo objects with metadata."""
        vfs = VirtualFS({})

        vfs.write("file1.txt", b"hello")
        vfs.write("file2.txt", b"world")
        vfs.write("dir/file3.txt", b"nested")

        # List root
        files = vfs.list_detailed("/")
        assert len(files) == 3  # file1.txt, file2.txt, dir

        # Find file1.txt
        file1 = next(f for f in files if f.name == "file1.txt")
        assert file1.size == 5
        assert file1.created_at
        assert file1.modified_at
        assert file1.is_dir is False

        # Find dir
        dir_item = next(f for f in files if f.name == "dir")
        assert dir_item.is_dir is True
        assert dir_item.size == 0  # Directories have size 0

    def test_list_detailed_subdirectory(self):
        """Test that list_detailed works for subdirectories."""
        vfs = VirtualFS({})

        vfs.write("dir/file1.txt", b"a")
        vfs.write("dir/file2.txt", b"bb")

        files = vfs.list_detailed("/dir")
        assert len(files) == 2

        file1 = next(f for f in files if f.name == "file1.txt")
        assert file1.size == 1
        assert file1.path == "dir/file1.txt"

        file2 = next(f for f in files if f.name == "file2.txt")
        assert file2.size == 2
        assert file2.path == "dir/file2.txt"

    def test_utime_updates_modified_at(self):
        """Test that utime() updates modification time in metadata."""
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")

        original = vfs.stat("file.txt")

        # Set mtime to a known timestamp (2020-01-01 00:00:00 UTC)
        vfs.utime("file.txt", (1577836800.0, 1577836800.0))

        updated = vfs.stat("file.txt")
        assert updated.modified_at != original.modified_at
        assert "2020-01-01" in updated.modified_at
        assert updated.created_at == original.created_at
        assert updated.size == original.size

    def test_utime_none_sets_current_time(self):
        """Test that utime(path, None) updates mtime to current time."""
        vfs = VirtualFS({})
        vfs.write("file.txt", b"hello")

        # Set to a past time first
        vfs.utime("file.txt", (1577836800.0, 1577836800.0))
        old = vfs.stat("file.txt")
        assert "2020-01-01" in old.modified_at

        # Now call with None — should update to current time
        vfs.utime("file.txt", None)
        new = vfs.stat("file.txt")
        assert "2020-01-01" not in new.modified_at

    def test_utime_missing_file_raises(self):
        """Test that utime() raises FileNotFoundError for missing files."""
        vfs = VirtualFS({})

        try:
            vfs.utime("missing.txt", None)
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass
