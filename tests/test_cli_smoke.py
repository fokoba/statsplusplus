"""Smoke tests: every CLI tool runs against a fixture league.

Guards the class of bug found in the Session 80 refactor audit — tools that
crash (or silently return nothing) on leagues that don't surface OVR/POT
(e.g. PPL stores NULL ovr/pot; the app uses its own composite/ceiling instead).

Each tool is run as a subprocess with STATSPP_LEAGUE pointed at an on-disk
fixture league, exercising the real invocation path (module-level context
resolution + query execution) rather than a mocked internal function.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from _fixture_league import build_fixture, remove_fixture

BASE = Path(__file__).resolve().parent.parent

_SLUGS = {"_smoke_ovr": True, "_smoke_noovr": False}


@pytest.fixture(scope="module")
def fixture_leagues():
    """Build normal + OVR-less fixture leagues under data/; clean up after."""
    for slug, with_ovr in _SLUGS.items():
        build_fixture(slug, with_ovr)
    try:
        yield list(_SLUGS.keys())
    finally:
        for slug in _SLUGS:
            remove_fixture(slug)


# Tools that read the DB and should exit cleanly on any league.
_TOOLS = [
    ["standings.py"],
    ["team_needs.py"],
    ["trade_assets.py"],
    ["trade_targets.py", "--bucket", "SP"],
    ["free_agents.py"],
    ["free_agents.py", "--my-team"],
    ["prospect_query.py", "top", "--n", "5"],
    ["roster_analysis.py"],
    ["farm_analysis.py"],
    ["benchmark.py"],
]


def _run_tool(tool_args, slug):
    env = dict(os.environ)
    env["STATSPP_LEAGUE"] = slug
    return subprocess.run(
        [sys.executable, str(BASE / "scripts" / tool_args[0]), *tool_args[1:]],
        env=env, capture_output=True, text=True, timeout=120, cwd=str(BASE),
    )


@pytest.mark.parametrize("slug_key", list(_SLUGS))
@pytest.mark.parametrize("tool_args", _TOOLS, ids=lambda a: "_".join(a).replace(".py", ""))
def test_cli_tool_runs_cleanly(fixture_leagues, tool_args, slug_key):
    """Every CLI tool exits 0 on both OVR-present and OVR-less fixture leagues."""
    proc = _run_tool(tool_args, slug_key)
    assert proc.returncode == 0, (
        f"{' '.join(tool_args)} on {slug_key} exited {proc.returncode}\n"
        f"STDERR:\n{proc.stderr[-2000:]}"
    )
