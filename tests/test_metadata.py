"""Tests for VirtualFS file metadata tracking."""

import json

from monkeyfs import VirtualFS


def rows(state):
    """Every metadata row in the state, keyed by the path it describes."""
    return {
        VirtualFS.path_for_metadata_key(key): json.loads(value)
        for key, value in state.items()
        if VirtualFS.is_metadata_key(key)
    }


def legacy_table(state):
    """The legacy single-table metadata, or None if the key is gone."""
    raw = state.get(VirtualFS.METADATA_KEY)
    return None if raw is None else json.loads(raw)


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

    def test_update_keeps_single_metadata_row(self):
        """Updating through an absolute path must not split the row.

        Regression: only creates normalized the metadata key, so a
        create-then-update through the same absolute path left a stale
        row beside the new one — stat went stale and consumers saw two
        rows for one file.
        """
        state: dict = {}
        vfs = VirtualFS(state)

        vfs.write("/workspace/x", b"file")
        vfs.write("/workspace/x", b"file main")

        assert vfs.stat("/workspace/x").size == 9
        assert sorted(k for k in rows(state) if "x" in k) == ["workspace/x"]

    def test_append_keeps_single_metadata_row(self):
        """Append mode must update the create row, not add one."""
        state: dict = {}
        vfs = VirtualFS(state)

        vfs.write("/workspace/x", b"file")
        created = vfs.stat("/workspace/x").created_at
        vfs.write("/workspace/x", b" more", mode="a")

        meta = vfs.stat("/workspace/x")
        assert meta.size == 9
        assert meta.created_at == created
        assert sorted(k for k in rows(state) if "x" in k) == ["workspace/x"]

    def test_relative_paths_roundtrip_under_cwd(self):
        """Relative writes, stat, utime and remove agree under a CWD."""
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.makedirs("/workspace")
        vfs.chdir("/workspace")

        vfs.write("x", b"file")
        vfs.write("x", b"file main")

        meta = vfs.stat("x")
        assert meta.size == 9
        assert meta.created_at != meta.modified_at  # real row, not synthetic
        assert sorted(k for k in rows(state) if "x" in k) == ["workspace/x"]

        vfs.utime("x")  # must touch the row, not duplicate it
        assert sorted(k for k in rows(state) if "x" in k) == ["workspace/x"]

        vfs.remove("x")  # must drop the row, not orphan it
        assert [k for k in rows(state) if "x" in k] == []

    def test_legacy_unresolved_row_still_found(self):
        """Table entries written before keys resolved keep working.

        The legacy table holds the raw normalized key while the blob
        lives under the resolved one; readers must find it, and a write
        must move it to the file's own row instead of splitting it.
        """
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.makedirs("/workspace")
        vfs.chdir("/workspace")

        key = vfs._encode_path("x")
        state[key] = b"data"
        stamp = "2026-01-01T00:00:00+00:00"
        state[VirtualFS.METADATA_KEY] = json.dumps(
            {
                "x": {
                    "size": 4,
                    "created_at": stamp,
                    "modified_at": stamp,
                    "is_dir": False,
                }
            }
        ).encode()
        # Surgery bypasses the cache; persisted state loads cold.
        vfs.invalidate()

        meta = vfs.stat("x")  # real entry, not synthetic timestamps
        assert (meta.size, meta.created_at) == (4, stamp)

        vfs.write("x", b"data!")  # adopts the legacy entry, no split
        assert sorted(k for k in rows(state) if "x" in k) == ["workspace/x"]
        assert rows(state)["workspace/x"]["size"] == 5
        assert rows(state)["workspace/x"]["created_at"] == stamp
        # The stale table entry went with it, and the key with it
        assert legacy_table(state) is None

        vfs.remove("x")
        assert [k for k in rows(state) if "x" in k] == []

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
        assert meta.size == 5
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
        assert file1.path == "/dir/file1.txt"

        file2 = next(f for f in files if f.name == "file2.txt")
        assert file2.size == 2
        assert file2.path == "/dir/file2.txt"

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


def _legacy_state(entries, blobs=None):
    """A state as monkeyfs 0.1.9 wrote it: blobs plus one metadata table."""
    state: dict = {}
    vfs = VirtualFS(state)
    for path, content in (blobs or {}).items():
        state[vfs._encode_path(path)] = content
    state[VirtualFS.METADATA_KEY] = json.dumps(entries).encode()
    return state


def _entry(size, stamp="2026-01-01T00:00:00+00:00", is_dir=False):
    return {
        "size": size,
        "created_at": stamp,
        "modified_at": stamp,
        "is_dir": is_dir,
    }


class TestKeyScheme:
    """A row and its blob are siblings under one encoding."""

    def test_row_key_is_the_blob_key_with_the_meta_prefix(self):
        vfs = VirtualFS({})
        blob = vfs._encode_path("dir/file.txt")
        row = vfs.metadata_key("dir/file.txt")

        assert blob.startswith(VirtualFS.PREFIX)
        assert row == VirtualFS.META_PREFIX + blob[len(VirtualFS.PREFIX) :]

    def test_round_trip_through_the_row_key(self):
        vfs = VirtualFS({})
        key = vfs.metadata_key("/dir/file.txt")
        assert VirtualFS.is_metadata_key(key)
        assert VirtualFS.path_for_metadata_key(key) == "dir/file.txt"

    def test_row_key_resolves_against_the_cwd(self):
        vfs = VirtualFS({})
        vfs.makedirs("/workspace")
        vfs.chdir("/workspace")
        assert vfs.metadata_key("x") == vfs.metadata_key("/workspace/x")

    def test_a_blob_key_is_never_a_row_key(self):
        vfs = VirtualFS({})
        for path in ("meta_x", "metadata", "a/b", "x"):
            assert not VirtualFS.is_metadata_key(vfs._encode_path(path))

    def test_the_legacy_table_key_is_not_a_row_key(self):
        assert not VirtualFS.is_metadata_key(VirtualFS.METADATA_KEY)
        assert not VirtualFS.is_metadata_key(VirtualFS.CWD_KEY)

    def test_path_for_metadata_key_rejects_a_blob_key(self):
        vfs = VirtualFS({})
        try:
            VirtualFS.path_for_metadata_key(vfs._encode_path("x"))
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


class TestRowsPerOperation:
    """Each mutating verb writes or removes exactly the rows it touched."""

    def test_write_adds_one_row_beside_the_blob(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a.txt", b"one")

        assert set(rows(state)) == {"a.txt"}
        assert state[vfs.metadata_key("a.txt")]
        assert legacy_table(state) is None

    def test_write_rewrites_only_its_own_row(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a.txt", b"one")
        vfs.write("b.txt", b"two")
        untouched = state[vfs.metadata_key("a.txt")]

        vfs.write("b.txt", b"two more")

        assert state[vfs.metadata_key("a.txt")] == untouched
        assert rows(state)["b.txt"]["size"] == 8

    def test_write_many_writes_one_row_per_file(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write_many({"a.txt": b"one", "d/b.txt": b"two"})

        assert set(rows(state)) == {"a.txt", "d/b.txt"}

    def test_remove_drops_the_row_with_the_blob(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a.txt", b"one")
        vfs.write("b.txt", b"two")

        vfs.remove("a.txt")

        assert set(rows(state)) == {"b.txt"}
        assert vfs.metadata_key("a.txt") not in state

    def test_remove_many_drops_every_row(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write_many({"a.txt": b"one", "b.txt": b"two", "c.txt": b"three"})

        vfs.remove_many(["a.txt", "b.txt"])

        assert set(rows(state)) == {"c.txt"}

    def test_mkdir_writes_a_directory_row(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.mkdir("d")

        assert rows(state)["d"]["is_dir"] is True
        assert rows(state)["d"]["size"] == 0

    def test_rmdir_drops_the_directory_row(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.mkdir("d")
        vfs.rmdir("d")

        assert rows(state) == {}

    def test_implicit_directories_get_no_row(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a/b/c.txt", b"x")

        # write() auto-creates the parents, so those are explicit; the
        # file's own row is the only non-directory one.
        assert {p for p, r in rows(state).items() if not r["is_dir"]} == {"a/b/c.txt"}

    def test_rename_moves_the_row_with_the_blob(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("old.txt", b"content")

        vfs.rename("old.txt", "new.txt")

        assert set(rows(state)) == {"new.txt"}

    def test_rename_directory_moves_every_row_under_it(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.mkdir("src")
        vfs.write("src/a.txt", b"one")
        vfs.write("src/sub/b.txt", b"two")

        vfs.rename("src", "dst")

        assert set(rows(state)) == {"dst", "dst/a.txt", "dst/sub", "dst/sub/b.txt"}

    def test_utime_rewrites_only_that_row(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a.txt", b"one")
        vfs.write("b.txt", b"two")
        untouched = state[vfs.metadata_key("b.txt")]

        vfs.utime("a.txt", (1_577_836_800.0, 1_577_836_800.0))

        assert "2020-01-01" in rows(state)["a.txt"]["modified_at"]
        assert state[vfs.metadata_key("b.txt")] == untouched

    def test_truncate_updates_the_row_size(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a.txt", b"abcdef")

        vfs.truncate("a.txt", 2)

        assert rows(state)["a.txt"]["size"] == 2


class TestReadsAgreeWithRows:
    """Every read path answers from the rows, and they agree with each other."""

    def test_stat_getsize_and_list_detailed_agree(self):
        state: dict = {}
        vfs = VirtualFS(state)
        vfs.write("a.txt", b"hello")
        vfs.write("d/b.txt", b"worldly")

        for path, name in (("a.txt", "a.txt"), ("d/b.txt", "b.txt")):
            row = rows(state)[path]
            meta = vfs.stat(path)
            assert (meta.size, meta.created_at, meta.modified_at) == (
                row["size"],
                row["created_at"],
                row["modified_at"],
            )
            assert vfs.getsize(path) == row["size"]

        detailed = {f.name: f for f in vfs.list_detailed("/d")}
        assert detailed["b.txt"].size == rows(state)["d/b.txt"]["size"]
        assert detailed["b.txt"].modified_at == rows(state)["d/b.txt"]["modified_at"]

    def test_list_detailed_reads_rows_under_a_cwd(self):
        vfs = VirtualFS({})
        vfs.write("/work/a.txt", b"hello")
        vfs.chdir("/work")

        info = vfs.list_detailed(".")[0]
        assert info.size == 5
        assert info.created_at == vfs.stat("a.txt").created_at

    def test_explicit_directory_reads_come_from_its_row(self):
        vfs = VirtualFS({})
        vfs.mkdir("d")

        assert vfs.isdir("d")
        assert vfs.exists("d")
        assert vfs.stat("d").is_dir is True
        assert vfs.list("/") == ["d"]

    def test_quota_accounting_sums_the_rows(self):
        state: dict = {}
        vfs = VirtualFS(state, max_size_mb=1)
        vfs.write("a.txt", b"x" * 100)
        vfs.write("d/b.txt", b"y" * 250)

        expected = sum(r["size"] for r in rows(state).values() if not r["is_dir"])
        assert vfs._get_current_size() == expected == 350

        vfs.remove("a.txt")
        assert vfs._get_current_size() == 250

    def test_quota_still_refuses_an_oversized_write(self):
        vfs = VirtualFS({}, max_size_mb=1)
        vfs.write("big.bin", b"x" * (1024 * 1024 - 10))
        try:
            vfs.write("more.bin", b"y" * 100)
            assert False, "Should have raised OSError"
        except OSError:
            pass


class TestRowsAreNotFiles:
    """A row is metadata, so no listing may surface it as a file."""

    def test_list_and_glob_never_surface_rows(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"one")
        vfs.write("d/b.txt", b"two")
        vfs.mkdir("empty")

        assert sorted(vfs.list("/", recursive=True)) == [
            "a.txt",
            "d",
            "d/b.txt",
            "empty",
        ]
        # fnmatch's "*" spans separators, which is glob()'s own behaviour;
        # what matters here is that no metadata row is among the matches.
        assert vfs.glob("*") == ["a.txt", "d/b.txt"]
        assert vfs.glob("*/*") == ["d/b.txt"]
        assert not [m for m in vfs.glob("*") if VirtualFS.is_metadata_key(m)]

    def test_a_row_is_not_a_file(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"one")
        row_path = VirtualFS.path_for_metadata_key(vfs.metadata_key("a.txt"))

        assert row_path == "a.txt"
        assert not vfs._is_vfs_key(vfs.metadata_key("a.txt"))
        assert vfs._is_vfs_key(vfs._encode_path("a.txt"))

    def test_snapshot_holds_paths_not_keys(self):
        vfs = VirtualFS({})
        vfs.write("a.txt", b"one")
        vfs.mkdir("d")

        assert set(vfs.get_metadata_snapshot()) == {"a.txt", "d"}


class TestLegacyTableMigration:
    """A 0.1.9 state reads as before and migrates one path at a time."""

    def test_table_is_read_as_a_fallback(self):
        state = _legacy_state(
            {"a.txt": _entry(3), "b.txt": _entry(3)},
            {"a.txt": b"one", "b.txt": b"two"},
        )
        vfs = VirtualFS(state)

        assert vfs.stat("a.txt").size == 3
        assert vfs.stat("a.txt").created_at == "2026-01-01T00:00:00+00:00"
        assert {f.name: f.size for f in vfs.list_detailed("/")} == {
            "a.txt": 3,
            "b.txt": 3,
        }

    def test_reading_never_writes(self):
        state = _legacy_state({"a.txt": _entry(3)}, {"a.txt": b"one"})
        before = dict(state)
        vfs = VirtualFS(state)

        vfs.stat("a.txt")
        vfs.list("/")
        vfs.list_detailed("/")
        vfs.get_metadata_snapshot()
        vfs.exists("a.txt")

        assert state == before

    def test_a_write_moves_one_path_out_of_the_table(self):
        state = _legacy_state(
            {"a.txt": _entry(3), "b.txt": _entry(3)},
            {"a.txt": b"one", "b.txt": b"two"},
        )
        vfs = VirtualFS(state)

        vfs.write("a.txt", b"one!")

        assert set(rows(state)) == {"a.txt"}
        assert rows(state)["a.txt"]["size"] == 4
        # created_at survives the move
        assert rows(state)["a.txt"]["created_at"] == "2026-01-01T00:00:00+00:00"
        assert set(legacy_table(state)) == {"b.txt"}

    def test_the_table_key_goes_when_the_last_entry_does(self):
        state = _legacy_state(
            {"a.txt": _entry(3), "b.txt": _entry(3)},
            {"a.txt": b"one", "b.txt": b"two"},
        )
        vfs = VirtualFS(state)

        vfs.write("a.txt", b"one!")
        vfs.write("b.txt", b"two!")

        assert set(rows(state)) == {"a.txt", "b.txt"}
        assert VirtualFS.METADATA_KEY not in state

    def test_a_remove_drains_the_table_too(self):
        state = _legacy_state(
            {"a.txt": _entry(3), "b.txt": _entry(3)},
            {"a.txt": b"one", "b.txt": b"two"},
        )
        vfs = VirtualFS(state)

        vfs.remove("a.txt")

        assert set(legacy_table(state)) == {"b.txt"}
        assert vfs.metadata_key("a.txt") not in state

    def test_a_row_wins_over_a_table_entry_for_the_same_path(self):
        state = _legacy_state({"a.txt": _entry(3)}, {"a.txt": b"one"})
        vfs = VirtualFS(state)
        # Surgery: a row and a stale table entry for one path, as a
        # half-migrated branch merged from an older writer would hold.
        state[vfs.metadata_key("a.txt")] = json.dumps(
            _entry(99, stamp="2026-06-06T00:00:00+00:00")
        ).encode()
        vfs.invalidate()

        assert vfs.stat("a.txt").size == 99
        assert vfs.stat("a.txt").created_at == "2026-06-06T00:00:00+00:00"
        assert vfs.get_metadata_snapshot()["a.txt"].size == 99

    def test_snapshot_merges_both_sources(self):
        state = _legacy_state(
            {"old.txt": _entry(3), "both.txt": _entry(3)},
            {"old.txt": b"one", "both.txt": b"two"},
        )
        vfs = VirtualFS(state)
        vfs.write("new.txt", b"three")

        snapshot = vfs.get_metadata_snapshot()

        assert set(snapshot) == {"old.txt", "both.txt", "new.txt"}
        assert snapshot["old.txt"].size == 3
        assert snapshot["new.txt"].size == 5

    def test_quota_counts_the_table_and_the_rows(self):
        state = _legacy_state({"old.txt": _entry(300)}, {"old.txt": b"x" * 300})
        vfs = VirtualFS(state, max_size_mb=1)
        vfs.write("new.txt", b"y" * 200)

        assert vfs._get_current_size() == 500

    def test_a_directory_row_migrates_like_a_file(self):
        state = _legacy_state({"d": _entry(0, is_dir=True)})
        vfs = VirtualFS(state)

        assert vfs.isdir("d")
        vfs.write("d/a.txt", b"one")  # touches d's row via makedirs? no: d exists

        assert vfs.isdir("d")
        assert set(rows(state)) == {"d/a.txt"}
        assert set(legacy_table(state)) == {"d"}
