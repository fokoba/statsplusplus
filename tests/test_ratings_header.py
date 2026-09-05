"""Unit tests for ratings CSV header repair/validation (no network).

Locks in two behaviors that a silent data-corruption bug depended on:
  1. Newer exports label the three Ctrl columns correctly (Ctrl/Ctrl_R/Ctrl_L)
     and must pass through UNCHANGED — the legacy label-repair must not fire.
  2. The legacy mislabeled export (Ctrl_R, Ctrl_L, Ctrl_L) must still be
     repaired to Ctrl, Ctrl_R, Ctrl_L.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from statsplus import client


def _header_after(cols: list[str]) -> list[str]:
    text = ",".join(cols) + "\n1"
    return client._fix_ratings_header(text).split("\n", 1)[0].split(",")


def test_new_127_header_passes_through_unchanged():
    """Correctly-labeled 127-col header must not be mangled by the Ctrl repair."""
    expected = client._RATINGS_KNOWN_FORMATS[127]
    result = _header_after(expected)
    assert result == expected
    # Exactly one of each Ctrl variant — no duplicate "Ctrl".
    assert result.count("Ctrl") == 1
    assert result.count("Ctrl_R") == 1
    assert result.count("Ctrl_L") == 1


def test_127_format_is_known():
    """127-col layout is registered so it does not trigger the change warning."""
    assert 127 in client._RATINGS_KNOWN_FORMATS
    fmt = client._RATINGS_KNOWN_FORMATS[127]
    assert len(fmt) == 127
    # PPL-style export: no OVR/POT/Prone.
    assert "Ovr" not in fmt and "Pot" not in fmt and "Prone" not in fmt
    # New fields present.
    for c in ("GBType", "FBType", "PotVel", "ArmSlot"):
        assert c in fmt


def test_legacy_mislabeled_header_is_repaired():
    """Legacy export mislabels overall Ctrl as Ctrl_R and duplicates Ctrl_L."""
    result = _header_after(["ID", "Ctrl_R", "Ctrl_L", "Ctrl_L"])
    assert result == ["ID", "Ctrl", "Ctrl_R", "Ctrl_L"]


def test_known_126_header_ctrl_intact():
    """126-col format already has a plain Ctrl and must be left alone."""
    expected = client._RATINGS_KNOWN_FORMATS[126]
    assert _header_after(expected) == expected


# --- _fetch: User-Agent + rate-limit handling (no real network) ---

import contextlib
import io
import urllib.request


class _FakeResp:
    def __init__(self, body: str, content_type: str = "text/csv"):
        self._body = body.encode()
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_sends_user_agent(monkeypatch):
    """StatsPlus bot filter serves a login page without a User-Agent — ensure we send one."""
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["headers"] = req.headers
        return _FakeResp("ok")

    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "sessionid=x", ""))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    body = client._fetch("https://example/test")
    assert body == "ok"
    # urllib title-cases header keys
    assert "User-agent" in captured["headers"]
    assert "statsplusplus" in captured["headers"]["User-agent"]


def test_fetch_retries_on_wait_message(monkeypatch):
    """A 'wait N seconds' body should trigger a retry, not be returned as data."""
    responses = iter([
        _FakeResp("please wait 1 seconds", content_type="text/plain"),
        _FakeResp("real-data"),
    ])

    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "sessionid=x", ""))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: next(responses))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    body = client._fetch("https://example/test")
    assert body == "real-data"



def test_fetch_uses_token_as_query_param(monkeypatch):
    """When a token is configured, it's appended to the URL and no Cookie header is sent."""
    captured = {}

    def fake_urlopen(req, *a, **k):
        captured["url"] = req.full_url
        captured["headers"] = req.headers
        return _FakeResp("ID,Name\n1,x")

    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "sessionid=x", "TOK123"))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client._fetch("https://statsplus.net/ppl/api/players")
    assert "token=TOK123" in captured["url"]
    # urllib title-cases header keys; Cookie must NOT be present when using a token.
    assert "Cookie" not in captured["headers"]


def test_fetch_appends_token_with_existing_query(monkeypatch):
    def fake_urlopen(req, *a, **k):
        fake_urlopen.url = req.full_url
        return _FakeResp("ok", content_type="text/csv")
    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "", "TOK"))
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client._fetch("https://statsplus.net/ppl/api/ratings?year=2034")
    assert "?year=2034&token=TOK" in fake_urlopen.url


def test_fetch_raises_on_expired_token(monkeypatch):
    """An HTTP-200 'API token has expired' body must raise, not be returned as data."""
    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "", "TOK"))
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: _FakeResp("API token has expired. Log in to refresh.",
                                       content_type="text/plain"))
    import pytest
    with pytest.raises(client.TokenExpiredError):
        client._fetch("https://example/ratings")


def test_fetch_raises_on_invalid_token(monkeypatch):
    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "", "BAD"))
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: _FakeResp("Invalid or unknown API token",
                                       content_type="text/plain"))
    import pytest
    with pytest.raises(client.TokenExpiredError):
        client._fetch("https://example/ratings")


def test_csv_content_type_not_treated_as_message(monkeypatch):
    """Real CSV data (text/csv) is returned even if a keyword appears — the
    content-type guard skips message classification for data responses."""
    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "c", ""))
    body = "ID,Note\n1,wait 5 seconds for the throw"
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, *a, **k: _FakeResp(body, content_type="text/csv"))
    assert client._fetch("https://example/players") == body


def test_start_ratings_export_raises_on_long_cooldown(monkeypatch):
    """A multi-minute /ratings cooldown surfaces as RateLimitedError rather than
    blocking the refresh for minutes."""
    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "c", ""))
    monkeypatch.setattr(client, "_base_url", lambda: "https://statsplus.net/ppl/api")
    monkeypatch.setattr(
        urllib.request, "urlopen",
        lambda req, *a, **k: _FakeResp("Request too soon, wait 240 seconds before requesting again",
                                       content_type="text/plain"))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    import pytest
    with pytest.raises(client.RateLimitedError) as ei:
        client.start_ratings_export()
    assert ei.value.seconds == 240
