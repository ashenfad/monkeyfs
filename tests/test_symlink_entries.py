"""Symlink fidelity for ``os.scandir()`` entries and the callers that trust them.

``shutil.rmtree()``, ``shutil.copytree()`` and ``os.walk()`` decide whether to
recurse from what a directory entry says about itself. A ``MockDirEntry`` that
answers ``is_symlink() -> False`` tells them a symlink is a plain directory, so
they walk through it into whatever it points at -- inside the sandbox, but
outside the subtree the caller named.
"""

import os
import shutil
import stat as stat_module

import pytest

from monkeyfs import IsolatedFS, VirtualFS, patch
from monkeyfs.mount import MountFS
from monkeyfs.readonly import ReadOnlyFS


def _sandbox(tmp_path):
    """Build a sandbox with a symlinked sibling directory and file.

    Layout, all inside the isolated root::

        root/tree/own.txt
        root/tree/inward   -> root/sibling   (symlink to a directory)
        root/tree/tofile   -> root/target.txt (symlink to a file)
        root/sibling/keep.txt
        root/target.txt

    ``rmtree("/tree")`` is entitled to delete ``tree`` and everything under it.
    ``sibling`` and ``target.txt`` are not under it.
    """
    root = tmp_path / "root"
    (root / "tree").mkdir(parents=True)
    (root / "tree" / "own.txt").write_text("own")
    (root / "sibling").mkdir()
    (root / "sibling" / "keep.txt").write_text("keep")
    (root / "target.txt").write_text("target")
    (root / "tree" / "inward").symlink_to(root / "sibling", target_is_directory=True)
    (root / "tree" / "tofile").symlink_to(root / "target.txt")
    return root


def _entries(path="/tree"):
    return {e.name: e for e in os.scandir(path)}


def _rmtree_holding_errors(root, path):
    """``rmtree(path)`` in a sandbox on ``root``, returning any error instead.

    Deleting the wrong directory and then failing are two different symptoms of
    the same bug, and the failure arrives last. Holding the exception lets a
    test assert on the damage first, so a red run names the data loss rather
    than a stray ``Directory not empty``.
    """
    with patch(IsolatedFS(str(root))):
        try:
            shutil.rmtree(path)
        except OSError as exc:
            return exc
    return None


class TestScandirEntryLinkness:
    """A scandir entry must report link-ness and honor ``follow_symlinks``."""

    def test_symlink_entries_report_is_symlink(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            entries = _entries()
            assert entries["inward"].is_symlink() is True
            assert entries["tofile"].is_symlink() is True
            assert entries["own.txt"].is_symlink() is False

    def test_link_to_dir_is_dir_only_when_following(self, tmp_path):
        """``is_dir(follow_symlinks=False)`` asks about the entry, not its target.

        This is the exact call ``shutil._rmtree_unsafe`` makes to decide
        whether to recurse.
        """
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            inward = _entries()["inward"]
            assert inward.is_dir() is True
            assert inward.is_dir(follow_symlinks=False) is False
            assert inward.is_file() is False
            assert inward.is_file(follow_symlinks=False) is False

    def test_link_to_file_is_file_only_when_following(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            tofile = _entries()["tofile"]
            assert tofile.is_file() is True
            assert tofile.is_file(follow_symlinks=False) is False
            assert tofile.is_dir() is False
            assert tofile.is_dir(follow_symlinks=False) is False

    def test_plain_entries_ignore_follow_symlinks(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            entries = _entries("/")
            assert entries["tree"].is_dir(follow_symlinks=False) is True
            assert entries["target.txt"].is_file(follow_symlinks=False) is True

    def test_entry_stat_follow_symlinks_false_reports_a_link(self, tmp_path):
        """``stat(follow_symlinks=False)`` is lstat: it describes the link."""
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            inward = _entries()["inward"]
            assert stat_module.S_ISDIR(inward.stat().st_mode)
            assert stat_module.S_ISLNK(inward.stat(follow_symlinks=False).st_mode)

            tofile = _entries()["tofile"]
            assert stat_module.S_ISREG(tofile.stat().st_mode)
            assert stat_module.S_ISLNK(tofile.stat(follow_symlinks=False).st_mode)

    def test_entry_stat_follow_symlinks_false_on_a_plain_file(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            own = _entries()["own.txt"]
            st = own.stat(follow_symlinks=False)
            assert stat_module.S_ISREG(st.st_mode)
            assert st.st_size == own.stat().st_size

    def test_is_junction_stays_false(self, tmp_path):
        """A junction is a Windows NTFS construct; a symlink is not one."""
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            entries = _entries()
            assert entries["inward"].is_junction() is False
            assert entries["own.txt"].is_junction() is False

    def test_readonly_wrapper_delegates_islink(self, tmp_path):
        """``ReadOnlyFS`` reaches ``islink()`` through its ``__getattr__``."""
        root = _sandbox(tmp_path)
        with patch(ReadOnlyFS(IsolatedFS(str(root)))):
            entries = _entries()
            assert entries["inward"].is_symlink() is True
            assert entries["inward"].is_dir(follow_symlinks=False) is False


class TestLstat:
    """``os.lstat()`` must not silently answer for the link's target."""

    def test_lstat_reports_the_link(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            assert stat_module.S_ISLNK(os.lstat("/tree/inward").st_mode)
            assert stat_module.S_ISDIR(os.stat("/tree/inward").st_mode)

    def test_pathlib_is_symlink(self, tmp_path):
        from pathlib import Path

        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            assert Path("/tree/inward").is_symlink() is True
            assert Path("/tree/own.txt").is_symlink() is False

    def test_lstat_on_a_plain_file_is_unchanged(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            st = os.lstat("/tree/own.txt")
            assert stat_module.S_ISREG(st.st_mode)
            assert st.st_size == 3


class TestRmtreeSymlinkContainment:
    """``rmtree`` must delete the link, never the tree it points at."""

    def test_rmtree_does_not_delete_a_symlinked_sibling(self, tmp_path):
        """The headline case: a link inside the tree, target outside it.

        ``shutil._rmtree_unsafe`` recurses when
        ``entry.is_dir(follow_symlinks=False)`` is true and
        ``entry.is_symlink()`` is false. With both answering for the link's
        target, rmtree walked through ``tree/inward`` and deleted
        ``root/sibling`` -- a directory the caller never named and which
        outlives the tree being removed.
        """
        root = _sandbox(tmp_path)
        # The rmtree error is held rather than raised so that the assertions
        # below report the damage first: a traceback out of rmtree says only
        # that it stopped, not that it took the sibling with it.
        error = _rmtree_holding_errors(root, "/tree")

        assert (root / "sibling").is_dir(), (
            "rmtree deleted the symlink target directory"
        )
        assert (root / "sibling" / "keep.txt").is_file(), (
            "rmtree deleted the contents of the symlink target"
        )
        assert (root / "sibling" / "keep.txt").read_text() == "keep"
        assert error is None, f"rmtree failed: {error!r}"
        assert not (root / "tree" / "inward").is_symlink(), (
            "the symlink itself survived"
        )
        assert not (root / "tree").exists(), "the named tree was not removed"

    def test_rmtree_unlinks_a_symlink_to_a_file(self, tmp_path):
        """The link goes; the file it points at stays."""
        root = _sandbox(tmp_path)
        error = _rmtree_holding_errors(root, "/tree")

        assert (root / "target.txt").is_file(), (
            "rmtree deleted the file the symlink pointed at"
        )
        assert (root / "target.txt").read_text() == "target"
        assert error is None, f"rmtree failed: {error!r}"
        assert not (root / "tree").exists()

    def test_rmtree_refuses_a_symlink_as_its_argument(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            with pytest.raises(OSError, match="symbolic link"):
                shutil.rmtree("/tree/inward")

        assert (root / "sibling" / "keep.txt").read_text() == "keep"

    def test_rmtree_leaves_a_target_outside_the_root_untouched(self, tmp_path):
        """A link out of the sandbox: confinement refuses, nothing outside dies.

        ``IsolatedFS`` cannot create this link -- ``symlink()`` validates the
        target -- so it is planted directly on the host, as an escape already
        present in a mounted directory would be.

        The escaping entry is dropped by ``scandir`` (its ``stat()`` raises
        ``PermissionError``) while ``listdir`` still reports it, so rmtree
        empties the tree and then fails to rmdir it. That disagreement is a
        separate confinement bug; what matters here is that nothing outside
        the root is touched.
        """
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        root = _sandbox(tmp_path)
        (root / "tree" / "outward").symlink_to(outside, target_is_directory=True)

        with patch(IsolatedFS(str(root))):
            with pytest.raises(OSError):
                shutil.rmtree("/tree")

        assert (outside / "secret.txt").read_text() == "secret"
        assert outside.is_dir()
        assert (root / "tree" / "outward").is_symlink()


class TestRemoveDoesNotFollowLinks:
    """``os.remove()`` on a link removes the link, as POSIX unlink does."""

    def test_remove_symlink_to_file_keeps_target(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            os.remove("/tree/tofile")

        assert not (root / "tree" / "tofile").is_symlink()
        assert (root / "target.txt").read_text() == "target"

    def test_remove_symlink_to_dir_keeps_target(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            os.remove("/tree/inward")

        assert not (root / "tree" / "inward").is_symlink()
        assert (root / "sibling" / "keep.txt").read_text() == "keep"

    def test_remove_plain_file_still_works(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            os.remove("/tree/own.txt")
            with pytest.raises(IsADirectoryError):
                os.remove("/sibling")

        assert not (root / "tree" / "own.txt").exists()


class TestCopytreeAndWalk:
    def test_copytree_symlinks_true_recreates_the_link(self, tmp_path):
        """With ``symlinks=True`` the copy gets a link, not a duplicated tree."""
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            shutil.copytree("/tree", "/copy", symlinks=True)

        assert (root / "copy" / "inward").is_symlink(), (
            "the symlink was copied through instead of recreated"
        )
        assert os.readlink(root / "copy" / "inward") == str(root / "sibling")
        assert (root / "copy" / "tofile").is_symlink()
        assert (root / "copy" / "own.txt").read_text() == "own"
        assert sorted(p.name for p in (root / "copy").iterdir()) == [
            "inward",
            "own.txt",
            "tofile",
        ]

    def test_copytree_symlinks_false_copies_through_the_link(self, tmp_path):
        """The default still resolves links, which is what ``symlinks=False`` means."""
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            shutil.copytree("/tree", "/copy")

        assert not (root / "copy" / "inward").is_symlink()
        assert (root / "copy" / "inward" / "keep.txt").read_text() == "keep"
        assert (root / "copy" / "tofile").read_text() == "target"

    def test_walk_bottom_up_does_not_descend_into_a_symlinked_dir(self, tmp_path):
        """``os.walk(topdown=False)`` decides with ``entry.is_symlink()``.

        The topdown branch of ``os.walk`` uses ``os.path.islink()``, which was
        already routed to the filesystem; the bottom-up branch trusts the
        directory entry, so it descended.
        """
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            bottom_up = [top for top, _, _ in os.walk("/tree", topdown=False)]
            top_down = [top for top, _, _ in os.walk("/tree")]

        assert bottom_up == ["/tree"]
        assert top_down == ["/tree"]

    def test_walk_followlinks_true_still_descends(self, tmp_path):
        root = _sandbox(tmp_path)
        with patch(IsolatedFS(str(root))):
            tops = [top for top, _, _ in os.walk("/tree", followlinks=True)]

        assert "/tree/inward" in tops


class TestLinklessBackends:
    """VirtualFS and MountFS have no symlinks; their entries must say so."""

    def _vfs(self):
        vfs = VirtualFS({})
        vfs.write("/tree/own.txt", b"own")
        vfs.write("/tree/sub/leaf.txt", b"leaf")
        return vfs

    def test_virtualfs_refuses_symlink(self):
        vfs = self._vfs()
        with pytest.raises(PermissionError):
            vfs.symlink("/tree/own.txt", "/tree/link")

    def test_virtualfs_entries_are_never_links(self):
        vfs = self._vfs()
        with patch(vfs):
            entries = _entries()
            assert entries["sub"].is_symlink() is False
            assert entries["sub"].is_dir(follow_symlinks=False) is True
            assert entries["own.txt"].is_file(follow_symlinks=False) is True
            assert stat_module.S_ISDIR(
                entries["sub"].stat(follow_symlinks=False).st_mode
            )

    def test_virtualfs_rmtree_still_removes_the_tree(self):
        vfs = self._vfs()
        with patch(vfs):
            shutil.rmtree("/tree")
        assert not vfs.isdir("/tree")

    def test_mountfs_refuses_symlink(self):
        fs = MountFS(self._vfs(), {"/mnt": self._vfs()})
        with pytest.raises(PermissionError):
            fs.symlink("/mnt/tree/own.txt", "/mnt/tree/link")

    def test_mountfs_entries_are_never_links(self):
        fs = MountFS(self._vfs(), {"/mnt": self._vfs()})
        with patch(fs):
            entries = _entries("/mnt/tree")
            assert entries["sub"].is_symlink() is False
            assert entries["sub"].is_dir(follow_symlinks=False) is True

    def test_backend_without_islink_still_scandirs(self):
        """``islink()`` is not part of the FileSystem protocol.

        A backend that implements only the protocol must keep working:
        entries fall back to "not a link" rather than the scandir raising.
        """

        class MinimalFS:
            def isdir(self, path):
                return path in ("/", "/d")

            def list(self, path=".", recursive=False):
                return ["f.txt"]

            def stat(self, path):
                from monkeyfs.base import FileMetadata

                return FileMetadata(size=1, created_at="", modified_at="")

        with patch(MinimalFS()):
            entries = _entries("/d")
            assert entries["f.txt"].is_symlink() is False
            assert entries["f.txt"].is_file(follow_symlinks=False) is True
