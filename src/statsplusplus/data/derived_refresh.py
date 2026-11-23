"""Refresh calculated ratings and dependent values after a local raw import."""
from pathlib import Path
import fcntl


def refresh_derived_ratings(league_dir):
    """Serialize local recalculation and let failures propagate for retry.

    The importing connection must have committed and closed first. A lock
    prevents two local upload requests from interleaving their calculations.
    """
    if league_dir is None:
        from statsplusplus.config.league_context import get_league_dir
        league_dir = get_league_dir()
    league_dir = Path(league_dir)
    from statsplusplus.data.db import init_schema
    from statsplusplus.data.evaluation_engine import run as evaluate
    from statsplusplus.data.fv_calc import run as value
    with (league_dir / '.derived-refresh.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        init_schema(league_dir)
        evaluate(league_dir=league_dir)
        value(league_dir=league_dir)
