"""Offseason finance settings — the money the GM has available to spend.

Rather than reconstruct OOTP's cash-flow-based budget math (starting balance +
revenue − expenses + owner cash, spread across pools and multiple years — which
we can't fully rebuild from stored data), we let the user enter the two figures
the game already computes and shows on the contract-offer screen:

  - ``fa_budget``  : "money for free agents"
  - ``ext_budget`` : "money for extensions"

These are authoritative (the game's own numbers) and can't drift from a formula.
Downstream tooling (the FA cart / roster builder) draws *down* from ``fa_budget``
as the user targets players.

Values are raw dollars in the league's own scale (no assumption of MLB
millions); ``None`` means "not set", visually distinct from an explicit 0.

Persistence mirrors ``draft_settings`` — per-league JSON in ``config/``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

DEFAULT_SETTINGS: dict[str, Any] = {
    "version": 2,
    "fa_budget": None,   # game's "money for free agents"
    "ext_budget": None,  # game's "money for extensions"
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _settings_path(league_dir: Path) -> Path:
    return league_dir / "config" / "finance_settings.json"


def load_settings(league_dir: Path) -> dict[str, Any]:
    """Load finance settings for a league, or return defaults.

    Corrupt/invalid files fall back to defaults so the UI always renders. Also
    migrates the pre-v2 shape (total_budget + pools) by discarding it — those
    inputs no longer exist and can't be meaningfully mapped to the game's
    authoritative FA/extension figures.
    """
    path = _settings_path(league_dir)
    if not path.exists():
        return _deep_copy(DEFAULT_SETTINGS)
    try:
        return _validate_and_normalize(json.loads(path.read_text()))
    except (json.JSONDecodeError, ValueError):
        return _deep_copy(DEFAULT_SETTINGS)


def save_settings(league_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Validate and write finance settings. Returns the normalized dict."""
    validated = _validate_and_normalize(settings)
    path = _settings_path(league_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(validated, indent=2) + "\n")
    return validated


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _deep_copy(d: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(d))  # type: ignore[no-any-return]


def _coerce_money(val: Any) -> Optional[float]:
    """Coerce a money value to a non-negative float, or None if unset/invalid.

    Blank strings and None mean "unset". Negative values clamp to 0.
    """
    if val is None or val == "":
        return None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    return max(0.0, f)


def _validate_and_normalize(data: Any) -> dict[str, Any]:
    """Coerce arbitrary input into a well-formed v2 settings dict.

    Ignores any legacy keys (total_budget, pools) from the pre-v2 shape.
    Raises only on a fundamentally wrong top-level type.
    """
    if not isinstance(data, dict):
        raise ValueError("Finance settings must be a dict")
    return {
        "version": 2,
        "fa_budget": _coerce_money(data.get("fa_budget")),
        "ext_budget": _coerce_money(data.get("ext_budget")),
    }


# ---------------------------------------------------------------------------
# Derived figures
# ---------------------------------------------------------------------------

def recommended_contract(
    proj_war: Optional[float],
    dpw: float,
    age: Optional[int] = None,
    min_sal: float = 0.0,
) -> dict[str, Any]:
    """A simple recommended free-agent contract: annual value, length, total.

    Deliberately minimal for now — ``aav = max(proj_war, 0) × $/WAR`` floored at
    the league minimum salary (every player costs at least the minimum, so a
    replacement-level FA is priced at ``min_sal``, not $0), and a coarse
    age-based length. This is a *value-based estimate used as a cost proxy*, NOT
    the player's actual demand (the game doesn't expose that). A later pass will
    refine it (market tax, pipeline-aware length, positional scarcity); the
    return shape is fixed so callers/UI don't change.

    Returns ``{"aav", "years", "total"}`` in raw league dollars. ``aav`` is the
    figure the FA cart draws down against the single-offseason ``fa_budget``;
    ``total`` is the multi-year commitment for context.
    """
    war = max(proj_war or 0.0, 0.0)
    aav = max(war * dpw, min_sal or 0.0)
    years = _default_years(age)
    return {"aav": round(aav), "years": years, "total": round(aav * years)}


def _default_years(age: Optional[int]) -> int:
    """Coarse default contract length by age. Younger FAs command longer deals;
    older ones get shorter. Placeholder curve — refined in a later pass."""
    if age is None:
        return 3
    if age <= 28:
        return 4
    if age <= 32:
        return 3
    if age <= 35:
        return 2
    return 1


def available_for_fa(settings: dict[str, Any], committed_spent: float = 0.0) -> Optional[float]:
    """Free-agent money still available after cart commitments.

    ``fa_budget`` is the game's authoritative "money for free agents";
    ``committed_spent`` is what the FA cart has drawn down (0 until the cart
    exists). Returns None when no FA budget is set. May go negative if the user
    over-commits (real information, not an error).
    """
    fa = settings.get("fa_budget")
    if fa is None:
        return None
    return float(fa) - (committed_spent or 0.0)
