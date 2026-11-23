"""Save coherent raw and calculated ratings at their actual game dates."""
from __future__ import annotations


def snapshot_current_ratings(conn) -> int:
    """Upsert each latest rating on its own date; never backdate new scores.

    Copying the shared columns also preserves missing values as missing. This
    runs after evaluation, so the raw tools and calculated grades are from the
    same record. The caller owns the transaction.
    """
    history = [r[1] for r in conn.execute('PRAGMA table_info(ratings_history)')]
    ratings = {r[1] for r in conn.execute('PRAGMA table_info(ratings)')}
    if not history:
        return 0
    cols = [c for c in history if c in ratings]
    if not {'player_id', 'snapshot_date'} <= set(cols):
        return 0
    quoted = ', '.join(f'"{c}"' for c in cols)
    select = ', '.join(f'r."{c}"' for c in cols)
    updates = ', '.join(f'"{c}"=excluded."{c}"' for c in cols
                        if c not in ('player_id', 'snapshot_date'))
    before = conn.total_changes
    conn.execute(f'''
        INSERT INTO ratings_history ({quoted})
        SELECT {select} FROM ratings r
        WHERE r.snapshot_date=(SELECT MAX(r2.snapshot_date) FROM ratings r2
                               WHERE r2.player_id=r.player_id)
        ON CONFLICT(player_id, snapshot_date) DO UPDATE SET {updates}
    ''')
    return conn.total_changes - before
