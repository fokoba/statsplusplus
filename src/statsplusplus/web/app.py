"""Flask application factory.

Creates and configures the Flask app with:
- Request-scoped DB connection (single connection per request, auto-closed)
- League context middleware (sets g.league_dir, g.league_config)
- Template filters (fmt_ip, short_name, money)
- Error handling

Usage:
    from statsplusplus.web.app import create_app
    app = create_app()
    app.run(port=5001, debug=True)
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask, g, redirect, request
import werkzeug.exceptions

from statsplusplus.utils.formatting import fmt_money, fmt_ip, short_name
from statsplusplus.utils.logging import get_logger
from statsplusplus.web.context import close_conn

log = get_logger("web")


def create_app(project_root: Path | None = None) -> Flask:
    """Create and configure the Flask application.

    Args:
        project_root: Project root directory. Auto-detected if None.

    Returns:
        Configured Flask app ready to serve.
    """
    if project_root is None:
        # Auto-detect: this file is at src/statsplusplus/web/app.py
        project_root = Path(__file__).resolve().parent.parent.parent.parent

    # Flask app with templates/static from the legacy web/ directory
    # (during migration, templates remain in their current location)
    template_dir = project_root / "web" / "templates"
    static_dir = project_root / "web" / "static"

    app = Flask(
        __name__,
        template_folder=str(template_dir),
        static_folder=str(static_dir),
    )
    app.json.sort_keys = False
    app.jinja_env.policies["json.dumps_kwargs"] = {"sort_keys": False}

    # --- Template filters ---
    app.jinja_env.filters["fmt_ip"] = fmt_ip
    app.jinja_env.filters["short"] = short_name
    app.jinja_env.filters["money"] = fmt_money

    # --- Register blueprints ---
    from statsplusplus.web.routes.team import bp as team_bp
    from statsplusplus.web.routes.league import bp as league_bp
    from statsplusplus.web.routes.player import bp as player_bp

    app.register_blueprint(team_bp)
    app.register_blueprint(league_bp)
    app.register_blueprint(player_bp)

    # --- Request lifecycle ---

    _EXEMPT_PREFIXES = (
        "/settings", "/onboard", "/switch-league", "/refresh",
        "/static", "/api/test-connection", "/api/game-date",
        "/api/wipe-league",
    )

    @app.before_request
    def _set_league_context() -> None | werkzeug.wrappers.Response:
        """Populate Flask g with league-scoped config."""
        from statsplusplus.config.league_context import get_active_league_slug, get_league_dir
        from statsplusplus.config.league_config import LeagueConfig

        slug = get_active_league_slug()
        league_dir = get_league_dir(slug)
        settings_exist = (league_dir / "config" / "league_settings.json").exists()

        if not settings_exist and not request.path.startswith(("/onboard", "/static")):
            return redirect("/onboard")

        cfg = LeagueConfig(base_dir=league_dir)
        g.league_slug = slug
        g.league_dir = league_dir
        g.league_config = cfg


        g.league_ready = (league_dir / "league.db").exists() and (
            league_dir / "config" / "league_averages.json"
        ).exists()

        if not g.league_ready and not any(
            request.path.startswith(p) for p in _EXEMPT_PREFIXES
        ):
            return redirect("/settings")
        return None

    @app.teardown_request
    def _teardown(exc: BaseException | None) -> None:
        """Close DB connection and log errors."""
        close_conn(exc)
        if exc and not isinstance(exc, werkzeug.exceptions.HTTPException):
            log.error("Request teardown error: %s", exc, exc_info=exc)

    @app.errorhandler(Exception)
    def _handle_exception(e: Exception) -> werkzeug.wrappers.Response:
        if isinstance(e, werkzeug.exceptions.HTTPException):
            return e  # type: ignore[return-value]
        log.error(
            "Unhandled exception on %s %s: %s",
            request.method, request.path, e, exc_info=True,
        )
        raise e

    # --- Context processors ---

    @app.context_processor
    def _inject_globals() -> dict:
        """Inject global template variables."""
        import json as _json
        from statsplusplus.config.league_context import APP_CONFIG_PATH

        cfg = g.league_config if hasattr(g, "league_config") else None
        slug = cfg.settings.get("statsplus_slug", "") if cfg else ""

        # Discover all leagues for the switcher
        data_dir = APP_CONFIG_PATH.parent
        league_list = []
        if data_dir.exists():
            for d in sorted(data_dir.iterdir()):
                if d.is_dir() and (d / "config" / "league_settings.json").exists():
                    ls = _json.loads((d / "config" / "league_settings.json").read_text())
                    league_list.append({"slug": d.name, "name": ls.get("league", d.name)})

        return {
            "statsplus_base": f"https://statsplus.net/{slug}",
            "all_teams": sorted(cfg.team_names_map.items(), key=lambda x: x[1]) if cfg and getattr(g, "league_ready", False) else [],
            "league_name": cfg.settings.get("league", "League") if cfg else "League",
            "league_list": league_list,
            "active_league_slug": getattr(g, "league_slug", ""),
            "league_ready": getattr(g, "league_ready", False),
        }

    return app


def main() -> None:
    """Entry point for running the web server."""
    app = create_app()
    app.run(host="0.0.0.0", port=5001, debug=True)
