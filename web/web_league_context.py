"""
web/league_context.py — request-scoped league context accessors.

In Flask request context, reads from `g`. Outside Flask (scripts), falls back
to the default league resolution.

Usage in query modules:
    from web_league_context import get_db, get_cfg, get_team_abbr_map, ...

NOTE: get_db() now returns a request-scoped connection (cached on g) rather
than opening a new connection on every call. This is the key performance
optimization — a single page load now uses ONE connection instead of 17+.
The connection is closed automatically on request teardown via web/context.py.
"""

from flask import g, has_request_context


def get_db():
    """Get the request-scoped DB connection for the current league.

    Returns a SharedConnection wrapper that isolates row_factory changes.
    Each function can set conn.row_factory = None without affecting other
    functions sharing the same underlying connection.

    The wrapper resets row_factory to sqlite3.Row before each execute() call
    unless the function has explicitly set it to None for that scope.
    """
    import sqlite3

    if has_request_context() and hasattr(g, "league_dir"):
        if not hasattr(g, "_db_conn") or g._db_conn is None:
            import db as _db
            raw_conn = _db.get_conn(g.league_dir)
            g._db_conn = raw_conn
        # Return a scoped view that tracks its own row_factory
        return _ScopedConnection(g._db_conn)
    import db as _db
    return _db.get_conn()


class _ScopedConnection:
    """Wrapper around a shared sqlite3.Connection that isolates row_factory.

    Each instance tracks its own row_factory setting without mutating the
    underlying connection. This allows multiple functions to share one
    connection while independently choosing tuple vs Row access.
    """

    __slots__ = ("_conn", "_row_factory")

    def __init__(self, conn):
        self._conn = conn
        self._row_factory = conn.row_factory  # Start with connection default (Row)

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, value):
        self._row_factory = value

    def execute(self, sql, parameters=()):
        cur = self._conn.cursor()
        cur.row_factory = self._row_factory
        return cur.execute(sql, parameters)

    def executemany(self, sql, seq_of_parameters):
        cur = self._conn.cursor()
        cur.row_factory = self._row_factory
        return cur.executemany(sql, seq_of_parameters)

    def executescript(self, sql_script):
        return self._conn.executescript(sql_script)

    def commit(self):
        return self._conn.commit()

    def close(self):
        pass  # No-op — shared connection closed by teardown

    def cursor(self):
        cur = self._conn.cursor()
        cur.row_factory = self._row_factory
        return cur

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass  # No-op — shared connection managed by teardown


def get_cfg():
    """Get the LeagueConfig for the current league."""
    if has_request_context() and hasattr(g, "league_config"):
        return g.league_config
    from league_config import config
    return config


# Convenience accessors — avoid repeated get_cfg() calls in hot paths
def team_abbr_map():
    return get_cfg().team_abbr_map

def team_names_map():
    return get_cfg().team_names_map

def team_div_map():
    return get_cfg().team_div_map

def mlb_team_ids():
    return get_cfg().mlb_team_ids

def level_map():
    return get_cfg().level_map

def pos_map():
    return get_cfg().pos_map

def pos_order():
    return get_cfg().pos_order

def pyth_exp():
    return get_cfg().pyth_exp

def year():
    return get_cfg().year

def my_team_id():
    return get_cfg().my_team_id


def has_extended_ratings():
    """Check if the ratings table has extended columns (babip, hra, pbabip, prone)."""
    if has_request_context() and hasattr(g, "_has_ext_ratings"):
        return g._has_ext_ratings
    conn = get_db()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
    result = "babip" in cols
    if has_request_context():
        g._has_ext_ratings = result
    return result


def league_averages():
    """Load league_averages.json for the current league, or return zeros."""
    import json
    cfg = get_cfg()
    path = cfg.league_dir / "config" / "league_averages.json"
    if path.exists():
        return json.loads(path.read_text())
    return {
        "year": cfg.year, "teams_in_sample": 0,
        "batting": {"avg": 0, "obp": 0, "slg": 0, "ops": 0, "woba": 0,
                     "babip": 0, "iso": 0, "k_pct": 0, "bb_pct": 0},
        "pitching": {"era": 0, "fip": 0, "x_fip": 0, "k_pct": 0, "bb_pct": 0,
                      "k_bb_pct": 0, "babip": 0, "avg": 0, "obp": 0},
        "dollar_per_war": 0,
    }
