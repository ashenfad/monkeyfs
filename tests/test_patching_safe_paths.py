"""Tests for read-only host passthrough path boundaries."""

import os

import pytest

from monkeyfs import VirtualFS, patch
from monkeyfs.patching import core


def test_safe_system_path_requires_a_path_boundary(tmp_path, monkeypatch):
    safe_dir = tmp_path / "python"
    safe_dir.mkdir()
    safe_child = safe_dir / "lib" / "module.py"
    safe_child.parent.mkdir()
    safe_child.write_text("safe")

    prefix_sibling = tmp_path / "python-backup"
    prefix_sibling.mkdir()
    sibling_file = prefix_sibling / "secret.py"
    sibling_file.write_text("host secret")

    monkeypatch.setattr(core, "_SAFE_SYSTEM_PATHS", [str(safe_dir.resolve())])

    assert core._is_safe_system_path(safe_dir)
    assert core._is_safe_system_path(safe_child)
    assert not core._is_safe_system_path(prefix_sibling)
    assert not core._is_safe_system_path(sibling_file)


def test_safe_system_path_resolves_normalized_and_symlink_paths(tmp_path, monkeypatch):
    safe_dir = tmp_path / "python"
    safe_dir.mkdir()
    safe_child = safe_dir / "lib" / "module.py"
    safe_child.parent.mkdir()
    safe_child.write_text("safe")

    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.py"
    outside_file.write_text("host secret")

    safe_link = tmp_path / "python-link"
    escape_link = safe_dir / "outside-link"
    try:
        safe_link.symlink_to(safe_dir, target_is_directory=True)
        escape_link.symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks are unavailable: {exc}")

    monkeypatch.setattr(core, "_SAFE_SYSTEM_PATHS", [str(safe_dir.resolve())])

    assert core._is_safe_system_path(safe_dir / "lib" / ".." / "lib" / "module.py")
    assert core._is_safe_system_path(safe_link / "lib" / "module.py")
    assert not core._is_safe_system_path(escape_link / "secret.py")


def test_host_read_passthrough_rejects_prefix_sibling(tmp_path, monkeypatch):
    safe_dir = tmp_path / "python"
    safe_dir.mkdir()
    safe_file = safe_dir / "stdlib.py"
    safe_file.write_text("safe")

    prefix_sibling = tmp_path / "python-private"
    prefix_sibling.mkdir()
    sibling_file = prefix_sibling / "secret.py"
    sibling_file.write_text("host secret")

    monkeypatch.setattr(core, "_SAFE_SYSTEM_PATHS", [str(safe_dir.resolve())])

    with patch(VirtualFS({})):
        assert os.listdir(safe_dir) == ["stdlib.py"]
        assert safe_file.read_text() == "safe"
        assert not sibling_file.exists()
        with pytest.raises(FileNotFoundError):
            sibling_file.read_text()
