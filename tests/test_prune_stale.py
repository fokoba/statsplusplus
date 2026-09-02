"""Tests for prune_stale.prune — manifest-based stale-file cleanup.

This code deletes files from a user's install, so its safety properties are
tested explicitly: only prunes .py under tracked code dirs, only when a manifest
exists, and never touches data/, config, or non-.py files.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# prune_stale.py lives at the project root, not in tests/ or the package.
_ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("prune_stale", _ROOT / "prune_stale.py")
prune_stale = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(prune_stale)


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _make_install(tmp_path: Path, manifest_lines: list[str] | None) -> Path:
    """Build a fake install tree; write MANIFEST.txt if lines provided."""
    root = tmp_path / "install"
    root.mkdir()
    if manifest_lines is not None:
        (root / "MANIFEST.txt").write_text("\n".join(manifest_lines) + "\n")
    return root


def test_prunes_stale_py_not_in_manifest(tmp_path):
    root = _make_install(tmp_path, ["scripts/keep.py", "web/app.py"])
    _touch(root / "scripts" / "keep.py")
    _touch(root / "scripts" / "stale.py")   # not in manifest → prune
    _touch(root / "web" / "app.py")

    removed = prune_stale.prune(root)

    assert removed == ["scripts/stale.py"]
    assert not (root / "scripts" / "stale.py").exists()
    assert (root / "scripts" / "keep.py").exists()
    assert (root / "web" / "app.py").exists()


def test_no_manifest_is_noop(tmp_path):
    """Dev checkouts have no MANIFEST.txt — nothing must be deleted."""
    root = _make_install(tmp_path, manifest_lines=None)
    _touch(root / "scripts" / "anything.py")

    removed = prune_stale.prune(root)

    assert removed == []
    assert (root / "scripts" / "anything.py").exists()


def test_never_touches_data_dir(tmp_path):
    """User data under data/ must never be pruned even if not in the manifest."""
    root = _make_install(tmp_path, ["scripts/keep.py"])
    _touch(root / "scripts" / "keep.py")
    _touch(root / "data" / "ppl" / "some_script.py")  # a .py under data/

    removed = prune_stale.prune(root)

    assert removed == []
    assert (root / "data" / "ppl" / "some_script.py").exists()


def test_never_touches_non_py_files(tmp_path):
    """Only .py files are in scope — configs, data, docs are untouched."""
    root = _make_install(tmp_path, ["scripts/keep.py"])
    _touch(root / "scripts" / "keep.py")
    _touch(root / "scripts" / "notes.txt")
    _touch(root / "scripts" / "config.json")

    removed = prune_stale.prune(root)

    assert removed == []
    assert (root / "scripts" / "notes.txt").exists()
    assert (root / "scripts" / "config.json").exists()


def test_only_prunes_tracked_dirs(tmp_path):
    """A stray .py outside PRUNE_DIRS (e.g. root or docs/) is left alone."""
    root = _make_install(tmp_path, ["scripts/keep.py"])
    _touch(root / "scripts" / "keep.py")
    _touch(root / "some_root_script.py")       # root, not a tracked dir
    _touch(root / "docs" / "example.py")        # docs/ not tracked

    removed = prune_stale.prune(root)

    assert removed == []
    assert (root / "some_root_script.py").exists()
    assert (root / "docs" / "example.py").exists()


def test_windows_style_manifest_paths(tmp_path):
    """Manifest paths with backslashes normalize to forward slashes."""
    root = _make_install(tmp_path, ["scripts\\keep.py"])
    _touch(root / "scripts" / "keep.py")
    _touch(root / "scripts" / "stale.py")

    removed = prune_stale.prune(root)

    assert removed == ["scripts/stale.py"]
    assert (root / "scripts" / "keep.py").exists()


def test_prunes_nested_stale_file(tmp_path):
    root = _make_install(tmp_path, ["src/statsplusplus/data/db.py"])
    _touch(root / "src" / "statsplusplus" / "data" / "db.py")
    _touch(root / "src" / "statsplusplus" / "data" / "old_module.py")

    removed = prune_stale.prune(root)

    assert removed == ["src/statsplusplus/data/old_module.py"]
    assert (root / "src" / "statsplusplus" / "data" / "db.py").exists()
