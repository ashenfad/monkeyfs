"""Tests for VirtualFS core functionality."""

import pytest

from monkeyfs import VirtualFS


class TestVirtualFSBasics:
    """Test basic VirtualFS operations."""

    def test_write_and_read_file(self):
        """Test writing and reading a file."""
        vfs = VirtualFS({})

        # Write
        vfs.write("test.txt", b"Hello, World!")

        # Read
        content = vfs.read("test.txt")
        assert content == b"Hello, World!"

    def test_file_not_found(self):
        """Test reading non-existent file raises FileNotFoundError."""
        vfs = VirtualFS({})

        with pytest.raises(FileNotFoundError):
            vfs.read("nonexistent.txt")

    def test_list_files(self):
        """Test listing files in root directory."""
        vfs = VirtualFS({})

        vfs.write("file1.txt", b"content1")
        vfs.write("file2.txt", b"content2")
        vfs.write("dir/file3.txt", b"content3")

        files = vfs.list("/")
        assert sorted(files) == ["dir", "file1.txt", "file2.txt"]

    def test_list_subdirectory(self):
        """Test listing files in subdirectory."""
        vfs = VirtualFS({})

        vfs.write("data/file1.csv", b"a,b,c")
        vfs.write("data/file2.csv", b"x,y,z")
        vfs.write("other/file3.txt", b"text")

        files = vfs.list("data")
        assert sorted(files) == ["file1.csv", "file2.csv"]

    def test_list_nonexistent_raises(self):
        """Test listing a nonexistent directory raises FileNotFoundError."""
        vfs = VirtualFS({})
        with pytest.raises(FileNotFoundError):
            vfs.list("nope")

    def test_list_file_raises(self):
        """Test listing a file raises NotADirectoryError."""
        vfs = VirtualFS({})
        vfs.write("file.txt", b"data")
        with pytest.raises(NotADirectoryError):
            vfs.list("file.txt")

    def test_exists(self):
        """Test checking file existence."""
        vfs = VirtualFS({})

        vfs.write("exists.txt", b"content")

        assert vfs.exists("exists.txt") is True
        assert vfs.exists("nonexistent.txt") is False

    def test_exists_directory(self):
        """Test checking directory existence (implicit)."""
        vfs = VirtualFS({})

        vfs.write("data/file.csv", b"content")

        assert vfs.exists("data") is True
        assert vfs.exists("nonexistent_dir") is False

    def test_isfile(self):
        """Test isfile checks."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"content")
        vfs.write("dir/nested.txt", b"nested")

        assert vfs.isfile("file.txt") is True
        assert vfs.isfile("dir") is False
        assert vfs.isfile("nonexistent") is False

    def test_isdir(self):
        """Test isdir checks (implicit directories)."""
        vfs = VirtualFS({})

        vfs.write("dir/file.txt", b"content")

        assert vfs.isdir("dir") is True
        assert vfs.isdir("dir/file.txt") is False
        assert vfs.isdir("/") is True

    def test_getsize(self):
        """Test getting file size."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"Hello!")

        assert vfs.getsize("file.txt") == 6

    def test_remove(self):
        """Test removing a file."""
        vfs = VirtualFS({})

        vfs.write("temp.txt", b"temp")
        assert vfs.exists("temp.txt") is True

        vfs.remove("temp.txt")
        assert vfs.exists("temp.txt") is False

    def test_remove_nonexistent(self):
        """Test removing non-existent file raises error."""
        vfs = VirtualFS({})

        with pytest.raises(FileNotFoundError):
            vfs.remove("nonexistent.txt")

    def test_rename(self):
        """Test renaming a file."""
        vfs = VirtualFS({})

        vfs.write("old.txt", b"content")
        vfs.rename("old.txt", "new.txt")

        assert vfs.exists("old.txt") is False
        assert vfs.exists("new.txt") is True
        assert vfs.read("new.txt") == b"content"

    def test_write_append_mode(self):
        """Test writing in append mode."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"Line 1\n")
        vfs.write("file.txt", b"Line 2\n", mode="a")

        assert vfs.read("file.txt") == b"Line 1\nLine 2\n"

    def test_mkdir_creates_directory(self):
        """Test mkdir creates explicit directory entry."""
        vfs = VirtualFS({})

        # Should create directory
        vfs.mkdir("some_dir")

        # Should be accessible as a directory
        assert vfs.isdir("some_dir") is True
        assert vfs.exists("some_dir") is True
        assert vfs.isfile("some_dir") is False

    def test_makedirs_creates_tree(self):
        """Test makedirs creates parent directories."""
        vfs = VirtualFS({})

        # Creates entire tree
        vfs.makedirs("some/deep/path", exist_ok=True)

        # All levels should be directories
        assert vfs.isdir("some") is True
        assert vfs.isdir("some/deep") is True
        assert vfs.isdir("some/deep/path") is True


class TestVirtualFSPaths:
    """Test path handling in VirtualFS."""

    def test_path_normalization(self):
        """Test that leading slashes are handled correctly."""
        vfs = VirtualFS({})

        vfs.write("/file.txt", b"content")
        assert vfs.read("file.txt") == b"content"
        assert vfs.exists("/file.txt") is True

    def test_nested_paths(self):
        """Test deeply nested paths."""
        vfs = VirtualFS({})

        vfs.write("a/b/c/d/file.txt", b"deep")

        assert vfs.read("a/b/c/d/file.txt") == b"deep"
        assert vfs.exists("a/b/c/d") is True
        assert vfs.isdir("a/b/c") is True

    def test_path_encoding_roundtrip(self):
        """Test that path encoding/decoding is reversible."""
        vfs = VirtualFS({})

        paths = [
            "file.txt",
            "dir/file.txt",
            "deep/nested/path/file.csv",
            "special-chars_123.txt",
        ]

        for path in paths:
            encoded = vfs._encode_path(path)
            decoded = vfs._decode_path(encoded)
            assert decoded == path.lstrip("/") or decoded == "/"


class TestVirtualFSOpen:
    """Test VirtualFS.open() method."""

    def test_open_read_text(self):
        """Test opening file in text read mode."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"Hello, text!")

        with vfs.open("file.txt", "r") as f:
            content = f.read()

        assert content == "Hello, text!"
        assert isinstance(content, str)

    def test_open_read_binary(self):
        """Test opening file in binary read mode."""
        vfs = VirtualFS({})

        vfs.write("file.bin", b"\x00\x01\x02\x03")

        with vfs.open("file.bin", "rb") as f:
            content = f.read()

        assert content == b"\x00\x01\x02\x03"
        assert isinstance(content, bytes)

    def test_open_write_text(self):
        """Test opening file in text write mode."""
        vfs = VirtualFS({})

        with vfs.open("file.txt", "w") as f:
            f.write("Hello, write!")

        assert vfs.read("file.txt") == b"Hello, write!"

    def test_open_write_binary(self):
        """Test opening file in binary write mode."""
        vfs = VirtualFS({})

        with vfs.open("file.bin", "wb") as f:
            f.write(b"\x04\x05\x06")

        assert vfs.read("file.bin") == b"\x04\x05\x06"

    def test_open_append_mode(self):
        """Test opening file in append mode."""
        vfs = VirtualFS({})

        vfs.write("file.txt", b"Line 1\n")

        with vfs.open("file.txt", "a") as f:
            f.write("Line 2\n")

        assert vfs.read("file.txt") == b"Line 1\nLine 2\n"

    def test_open_nonexistent_read(self):
        """Test opening non-existent file for reading raises error."""
        vfs = VirtualFS({})

        with pytest.raises(FileNotFoundError):
            vfs.open("nonexistent.txt", "r")

    def test_open_text_read_update_overwrites_at_cursor(self):
        """Text r+ starts at zero and persists in-place writes."""
        vfs = VirtualFS({})
        vfs.write("file.txt", b"abcdef")

        with vfs.open("file.txt", "r+") as f:
            assert f.tell() == 0
            assert f.read(2) == "ab"
            assert f.write("XY") == 2
            assert f.tell() == 4
            f.seek(0)
            assert f.read() == "abXYef"

        assert vfs.read("file.txt") == b"abXYef"

    def test_open_binary_read_update_overwrites_at_cursor(self):
        """Binary rb+ starts at zero and persists in-place writes."""
        vfs = VirtualFS({})
        vfs.write("file.bin", b"\x00\x01\x02\x03\x04")

        with vfs.open("file.bin", "rb+") as f:
            assert f.tell() == 0
            assert f.read(2) == b"\x00\x01"
            assert f.write(b"\xaa\xbb") == 2
            assert f.tell() == 4
            f.seek(0)
            assert f.read() == b"\x00\x01\xaa\xbb\x04"

        assert vfs.read("file.bin") == b"\x00\x01\xaa\xbb\x04"

    @pytest.mark.parametrize(
        ("update_mode", "later_mode", "initial", "later"),
        [
            ("r+", "w", b"old text", b"new text"),
            ("rb+", "wb", b"\x00old", b"\x00new"),
        ],
    )
    def test_clean_update_handle_does_not_overwrite_later_writer(
        self, update_mode, later_mode, initial, later
    ):
        """Closing an untouched update snapshot must not restore stale data."""
        vfs = VirtualFS({})
        vfs.write("file", initial)

        stale = vfs.open("file", update_mode)
        assert stale.read()

        with vfs.open("file", later_mode) as current:
            current.write(later.decode() if later_mode == "w" else later)

        stale.close()
        assert vfs.read("file") == later

    @pytest.mark.parametrize(
        ("mode", "content", "lines"),
        [
            ("r+", b"one\ntwo\nthree", ["one\n", "two\n", "three"]),
            ("rb+", b"one\ntwo\nthree", [b"one\n", b"two\n", b"three"]),
        ],
    )
    def test_open_read_update_line_apis(self, mode, content, lines):
        """Update handles retain line reads and iteration."""
        vfs = VirtualFS({})
        vfs.write("file", content)

        with vfs.open("file", mode) as f:
            assert f.readline() == lines[0]
            assert f.readlines() == lines[1:]
            f.seek(0)
            assert iter(f) is f
            assert next(f) == lines[0]
            assert list(f) == lines[1:]

    def test_open_read_update_writelines_generator_persists(self):
        """A consumed writelines generator still marks the handle dirty."""
        vfs = VirtualFS({})
        vfs.write("file", b"")

        with vfs.open("file", "r+") as f:
            f.writelines(line for line in ["one\n", "two\n"])

        assert vfs.read("file") == b"one\ntwo\n"

    @pytest.mark.parametrize(
        ("mode", "content", "size", "use_current_position", "expected"),
        [
            ("r+", b"abcdef", 3, True, b"abc"),
            ("rb+", b"\x00\x01\x02\x03", 2, False, b"\x00\x01"),
        ],
    )
    def test_open_read_update_truncate_persists(
        self, mode, content, size, use_current_position, expected
    ):
        """Truncate marks an update handle dirty and persists on close."""
        vfs = VirtualFS({})
        vfs.write("file", content)

        with vfs.open("file", mode) as f:
            if use_current_position:
                f.seek(size)
                assert f.truncate() == size
            else:
                assert f.truncate(size) == size

        assert vfs.read("file") == expected

    def test_empty_write_modes_preserve_creation_and_truncation(self):
        """Clean-close optimization retains w/x/a open-time mutations."""
        vfs = VirtualFS({})
        vfs.write("truncate.txt", b"old")

        with vfs.open("truncate.txt", "w"):
            pass
        with vfs.open("exclusive.txt", "x"):
            pass
        with vfs.open("append.txt", "a"):
            pass

        assert vfs.read("truncate.txt") == b""
        assert vfs.read("exclusive.txt") == b""
        assert vfs.read("append.txt") == b""

    @pytest.mark.parametrize("mode", ["r+", "rb+"])
    def test_open_read_update_requires_existing_file(self, mode):
        """Update mode without creation fails when the file is absent."""
        vfs = VirtualFS({})

        with pytest.raises(FileNotFoundError):
            vfs.open("nonexistent", mode)

    @pytest.mark.parametrize("mode", ["+", "q+"])
    def test_open_invalid_update_mode(self, mode):
        """A plus sign alone does not turn an invalid mode into a write mode."""
        vfs = VirtualFS({})

        with pytest.raises(ValueError, match="Invalid mode"):
            vfs.open("file.txt", mode)


class TestVirtualFile:
    """Test VirtualFile class behavior."""

    def test_write_and_close(self):
        """Test that content is persisted on close."""
        vfs = VirtualFS({})

        f = vfs.open("file.txt", "w")
        f.write("content")
        # Not yet persisted
        assert vfs.exists("file.txt") is False

        f.close()
        # Now persisted
        assert vfs.read("file.txt") == b"content"

    def test_context_manager(self):
        """Test VirtualFile works as context manager."""
        vfs = VirtualFS({})

        with vfs.open("file.txt", "w") as f:
            f.write("auto-close")

        # File should be closed and persisted
        assert vfs.read("file.txt") == b"auto-close"

    def test_write_to_closed_file_raises(self):
        """Test writing to closed file raises error."""
        vfs = VirtualFS({})

        f = vfs.open("file.txt", "w")
        f.close()

        with pytest.raises(ValueError, match="closed file"):
            f.write("too late")

    def test_multiple_writes(self):
        """Test multiple writes to same file object."""
        vfs = VirtualFS({})

        with vfs.open("file.txt", "w") as f:
            f.write("Line 1\n")
            f.write("Line 2\n")
            f.write("Line 3\n")

        assert vfs.read("file.txt") == b"Line 1\nLine 2\nLine 3\n"
