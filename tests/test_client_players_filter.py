"""tests/test_client_players_filter.py — /players?retired=0 param plumbing.

Unit-tests the client `get_players(retired=...)` query-string behavior without
hitting the network. The steady-state refresh passes retired=0 (skip retired
players for speed); the first refresh of a league passes None (full pull, so
retired players are captured once).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from statsplus import client


def _capture_url(monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "_fetch", lambda url, **kw: seen.setdefault("url", url) or "")
    monkeypatch.setattr(client, "_base_url", lambda: "https://x/api")
    return seen


def test_get_players_retired_zero_adds_param(monkeypatch):
    seen = _capture_url(monkeypatch)
    client.get_players(retired=0)
    assert seen["url"] == "https://x/api/players/?retired=0"


def test_get_players_default_omits_param(monkeypatch):
    seen = _capture_url(monkeypatch)
    client.get_players()
    assert seen["url"] == "https://x/api/players/"


def test_get_players_none_omits_param(monkeypatch):
    seen = _capture_url(monkeypatch)
    client.get_players(retired=None)
    assert seen["url"] == "https://x/api/players/"
