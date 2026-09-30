"""Tests for get_ratings resilience — the ratings export request ID can expire
server-side when a refresh runs long (deep retro leagues, heavy rate-limiting).
Regression guard for the PPL "0 ratings → empty draft/prospects" bug: the client
must re-request a fresh export rather than silently returning 0 rows.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from statsplusplus.client import statsplus as cl


_VALID_CSV = "ID,Name,Stf\n1,Test Pitcher,55\n"
_EXPIRED = "The request ID is no longer valid, please request again via starting API"


def test_expired_request_id_triggers_reexport(monkeypatch):
    """When the poll URL returns the expired-request-ID message, get_ratings
    re-requests a fresh export and parses the retry response."""
    calls = {"start": 0, "fetch": 0}

    def fake_start():
        calls["start"] += 1
        return f"http://poll/{calls['start']}"

    def fake_fetch(url, **kw):
        calls["fetch"] += 1
        # First poll: expired. Second poll (after re-export): valid CSV.
        return _EXPIRED if calls["fetch"] == 1 else _VALID_CSV

    monkeypatch.setattr(cl, "start_ratings_export", fake_start)
    monkeypatch.setattr(cl, "_fetch", fake_fetch)
    monkeypatch.setattr(cl.time, "sleep", lambda *_: None)

    rows = cl.get_ratings(poll_url="http://poll/0", skip_initial_wait=True)
    assert len(rows) == 1 and rows[0]["Name"] == "Test Pitcher"
    # Re-requested exactly once (the initial poll_url was supplied).
    assert calls["start"] == 1


def test_expired_twice_raises(monkeypatch):
    """A second expiry after re-export raises rather than looping/returning empty."""
    monkeypatch.setattr(cl, "start_ratings_export", lambda: "http://poll/new")
    monkeypatch.setattr(cl, "_fetch", lambda url, **kw: _EXPIRED)
    monkeypatch.setattr(cl.time, "sleep", lambda *_: None)

    with pytest.raises(RuntimeError, match="expired twice"):
        cl.get_ratings(poll_url="http://poll/0", skip_initial_wait=True)


# ---------------------------------------------------------------------------
# Proactive render pacing
# ---------------------------------------------------------------------------

def test_render_paths_are_paced(monkeypatch):
    """A render-limited endpoint (team stats) sleeps to respect the per-minute
    render window between consecutive requests, rather than firing early and
    eating a 429."""
    slept = []
    fake_now = {"t": 1000.0}
    monkeypatch.setattr(cl.time, "monotonic", lambda: fake_now["t"])
    monkeypatch.setattr(cl.time, "sleep", lambda s: (slept.append(s), fake_now.__setitem__("t", fake_now["t"] + s)))
    monkeypatch.setattr(cl, "_resolve_creds", lambda: ("base", "cookie", "tok"))

    class _Resp:
        headers = {"Content-Type": "text/csv"}
        def read(self): return b"tid\n1\n"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(cl.urllib.request, "urlopen", lambda req: _Resp())
    cl._last_render_ts = 0.0

    # First render request — no prior, no pacing wait.
    cl._fetch("http://x/teambatstats?year=1950")
    assert slept == []
    # Immediate second render — must pace ~one window.
    cl._fetch("http://x/teampitchstats?year=1950")
    assert slept and slept[0] >= cl._RENDER_MIN_INTERVAL - 1


def test_non_render_paths_not_paced(monkeypatch):
    """Non-render endpoints (players, lgdata) are never paced."""
    slept = []
    monkeypatch.setattr(cl.time, "monotonic", lambda: 1000.0)
    monkeypatch.setattr(cl.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(cl, "_resolve_creds", lambda: ("base", "cookie", "tok"))

    class _Resp:
        headers = {"Content-Type": "application/json"}
        def read(self): return b"[]"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(cl.urllib.request, "urlopen", lambda req: _Resp())
    cl._last_render_ts = 500.0  # a recent render

    cl._fetch("http://x/players")
    cl._fetch("http://x/lgdata")
    assert slept == []  # no pacing on non-render paths
