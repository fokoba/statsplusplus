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
    def __init__(self, body: str):
        self._body = body.encode()

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
    responses = iter([_FakeResp("please wait 1 seconds"), _FakeResp("real-data")])

    monkeypatch.setattr(client, "_resolve_creds", lambda: ("ppl", "sessionid=x", ""))
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, *a, **k: next(responses))
    monkeypatch.setattr(client.time, "sleep", lambda s: None)
    body = client._fetch("https://example/test")
    assert body == "real-data"

