"""
ratings.py — Rating normalization utilities.

MIGRATION NOTE: This module now delegates to statsplusplus.config.ratings
for the pure normalization functions. The module-level _ratings_scale global
is maintained for backward compatibility with web/app.py which sets it per
request. New code should use statsplusplus.config.ratings.norm(raw, scale)
directly, passing scale as a parameter.
"""

_ratings_scale = None  # set by init_ratings_scale() or auto-detected


def init_ratings_scale(scale="1-100"):
    """Set the module-level ratings scale. Called once at startup."""
    global _ratings_scale
    _ratings_scale = scale


def get_ratings_scale():
    """Return the current ratings scale ('1-100' or '20-80'). Public accessor."""
    global _ratings_scale
    if _ratings_scale is None:
        from statsplusplus.config.league_config import LeagueConfig
        _ratings_scale = LeagueConfig().ratings_scale
    return _ratings_scale


# Keep private alias for internal use
_get_ratings_scale = get_ratings_scale


def norm(raw):
    """Normalize a tool rating to 20-80 scouting scale, rounded to nearest 5."""
    from statsplusplus.config.ratings import norm as _norm
    return _norm(raw, get_ratings_scale())


def norm_continuous(raw):
    """Normalize a tool rating to continuous 20-80 scale WITHOUT rounding."""
    from statsplusplus.config.ratings import norm_continuous as _nc
    return _nc(raw, get_ratings_scale())


def norm_floor(raw, floor=20):
    """norm() with a numeric fallback for call sites that require a number."""
    return norm(raw) or floor
