"""Player page routes.

Handles /player/<pid> route.
"""

from __future__ import annotations

from flask import Blueprint, render_template, g

bp = Blueprint("player", __name__)


@bp.route("/player/<int:pid>")
def player(pid: int):
    """Player detail page — ratings, stats, percentiles, scouting."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parent.parent.parent.parent
    if str(_root / "scripts") not in sys.path:
        sys.path.insert(0, str(_root / "scripts"))
    if str(_root / "web") not in sys.path:
        sys.path.insert(0, str(_root / "web"))

    import player_queries
    import queries

    cfg = g.league_config if hasattr(g, "league_config") else None
    data = player_queries.get_player(pid)
    if not data:
        return "Player not found", 404

    league_name = cfg.settings.get("league", "League") if cfg else "League"
    team_name = data.get("team_name", "")
    team_id = data.get("team_id")

    breadcrumbs = [{"label": league_name, "url": "/league"}]
    if team_id and team_name:
        breadcrumbs.append({"label": team_name, "url": f"/team/{team_id}"})
    breadcrumbs.append({"label": data.get("name", "Player"), "url": f"/player/{pid}"})

    return render_template("player.html", p=data, breadcrumbs=breadcrumbs)
