"""Smoke tests for packaging interfaces.

These guard the class of bug that shipped silently after the package refactor:
entry points pointing at a non-existent ``main()``. Cheap to run, no I/O.

Parses ``[project.scripts]`` from pyproject.toml and asserts every target
resolves to a callable — nothing is mocked, the real modules are imported.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import pytest

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _entry_points() -> dict[str, str]:
    data = tomllib.loads(_PYPROJECT.read_text())
    return data.get("project", {}).get("scripts", {})


def test_pyproject_has_entry_points():
    eps = _entry_points()
    assert eps, "no [project.scripts] entry points found"


@pytest.mark.parametrize("name,target", sorted(_entry_points().items()))
def test_entry_point_resolves_to_callable(name: str, target: str):
    """Every `spp-*` entry point must resolve to a callable `main`."""
    module_path, _, attr = target.partition(":")
    assert attr, f"{name}: entry point '{target}' has no ':function' part"
    mod = importlib.import_module(module_path)
    fn = getattr(mod, attr, None)
    assert fn is not None, f"{name}: {module_path} has no attribute '{attr}'"
    assert callable(fn), f"{name}: {target} is not callable"
