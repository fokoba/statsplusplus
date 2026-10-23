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
    monkeypatch.setattr(cl.urllib.request, "urlopen", lambda req, **kw: _Resp())
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
    monkeypatch.setattr(cl.urllib.request, "urlopen", lambda req, **kw: _Resp())
    cl._last_render_ts = 500.0  # a recent render

    cl._fetch("http://x/players")
    cl._fetch("http://x/lgdata")
    assert slept == []  # no pacing on non-render paths


def test_stalled_request_is_retried_not_fatal(monkeypatch):
    """A stalled socket (TimeoutError from the bounded urlopen timeout) is
    treated as transient and retried, rather than hanging forever or aborting
    the whole refresh. Regression guard for the OHBL "refresh wedges silently
    at league averages" bug: urlopen used to be called with no timeout, so a
    slow team-stats render froze the pipeline with no log line and no error."""
    monkeypatch.setattr(cl.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cl, "_resolve_creds", lambda: ("base", "cookie", "tok"))

    class _Resp:
        headers = {"Content-Type": "application/json"}
        def read(self): return b"[]"
        def __enter__(self): return self
        def __exit__(self, *a): return False

    calls = {"n": 0}

    def flaky_urlopen(req, **kw):
        # The bounded timeout MUST be passed through on every call.
        assert kw.get("timeout") == cl._FETCH_TIMEOUT
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return _Resp()

    monkeypatch.setattr(cl.urllib.request, "urlopen", flaky_urlopen)

    # First attempt stalls, second succeeds — no exception propagates.
    assert cl._fetch("http://x/players") == "[]"
    assert calls["n"] == 2


def test_persistent_stall_raises_after_retries(monkeypatch):
    """If every attempt stalls, the timeout surfaces as an error after the
    retry budget is exhausted — loud failure, never an infinite hang."""
    monkeypatch.setattr(cl.time, "sleep", lambda *_: None)
    monkeypatch.setattr(cl, "_resolve_creds", lambda: ("base", "cookie", "tok"))

    def always_stall(req, **kw):
        raise TimeoutError("read timed out")

    monkeypatch.setattr(cl.urllib.request, "urlopen", always_stall)

    with pytest.raises(TimeoutError):
        cl._fetch("http://x/players")


# ---------------------------------------------------------------------------
# Per-refresh render-payload cache
# ---------------------------------------------------------------------------

def _count_fetch(monkeypatch):
    """Patch _fetch to count calls and return a stable payload per URL."""
    calls = {"n": 0, "urls": []}

    def fake_fetch(url, _retries=cl._RATE_LIMIT_MAX_RETRIES):
        calls["n"] += 1
        calls["urls"].append(url)
        return f"payload::{url}"

    monkeypatch.setattr(cl, "_fetch", fake_fetch)
    return calls


def test_render_cache_dedups_identical_render_within_scope(monkeypatch):
    """Inside a begin/clear_render_cache scope, a second identical render URL is
    served from the memo without a second _fetch (no second 62s pacing window).
    This is the core refresh speed-up: _upsert_team_stats, _detect_league_structure
    and _refresh_league_averages all want the same /teambatstats render."""
    calls = _count_fetch(monkeypatch)
    try:
        cl.begin_render_cache()
        r1 = cl._get("/teambatstats/", {"year": 2051, "split": 1})
        r2 = cl._get("/teambatstats/", {"year": 2051, "split": 1})
    finally:
        cl.clear_render_cache()
    assert r1 == r2
    assert calls["n"] == 1  # second call was a cache hit


def test_render_cache_distinct_urls_each_fetch(monkeypatch):
    """Different render params (e.g. the offseason year→year-1 fallback) are
    distinct keys and each still fetches once, then caches independently."""
    calls = _count_fetch(monkeypatch)
    try:
        cl.begin_render_cache()
        cl._get("/teambatstats/", {"year": 2051, "split": 1})   # empty season
        cl._get("/teambatstats/", {"year": 2050, "split": 1})   # fallback
        cl._get("/teambatstats/", {"year": 2050, "split": 1})   # reused by averages
    finally:
        cl.clear_render_cache()
    assert calls["n"] == 2  # two distinct years, the 2050 repeat was a hit


def test_render_cache_noop_without_scope(monkeypatch):
    """With no active scope, every request hits the network exactly as before —
    the cache must never cache across refreshes implicitly."""
    calls = _count_fetch(monkeypatch)
    cl.clear_render_cache()  # ensure no scope
    cl._get("/teambatstats/", {"year": 2051, "split": 1})
    cl._get("/teambatstats/", {"year": 2051, "split": 1})
    assert calls["n"] == 2  # no dedup without begin_render_cache()


def test_render_cache_ignores_non_render_paths(monkeypatch):
    """Even inside a scope, cheap already-rendered endpoints (lgdata, players)
    are never cached — they must always reflect live data."""
    calls = _count_fetch(monkeypatch)
    try:
        cl.begin_render_cache()
        cl._get("/lgdata/")
        cl._get("/lgdata/")
    finally:
        cl.clear_render_cache()
    assert calls["n"] == 2  # non-render paths bypass the cache


def test_clear_render_cache_prevents_cross_refresh_leak(monkeypatch):
    """A payload cached in one scope must not survive into the next scope — a
    later refresh is a new game state and must re-render."""
    calls = _count_fetch(monkeypatch)
    cl.begin_render_cache()
    cl._get("/teambatstats/", {"year": 2051, "split": 1})
    cl.clear_render_cache()
    cl.begin_render_cache()
    cl._get("/teambatstats/", {"year": 2051, "split": 1})
    cl.clear_render_cache()
    assert calls["n"] == 2  # re-fetched in the second scope
