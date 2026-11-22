"""Roster status sync from OOTP's manual exports.

The StatsPlus API refresh is normally what keeps `players` current. When it
lapses (expired token, offline), retired and released players stay listed on
their old organization and show up on pages such as Cut Candidates.

OOTP's "MLB player list" All Columns export lists every player in the MLB
organizations (ORG column) and the "All Free Agents" export lists every free
agent. For the organizations that appear in the player list, any player the
DB still has on that organization but who is in neither export has left
baseball; one who is in the free-agent export has been released.

Only organizations present in the export are touched, so players of clubs the
export doesn't cover (e.g. other leagues' teams) are left alone. The next
successful StatsPlus refresh overwrites whatever this sets.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from collections import Counter, defaultdict


def _ids_and_orgs(data: bytes) -> dict[int, str]:
    text = data.decode("utf-8-sig", errors="ignore")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    header = rows[0]
    org_cols = [i for i, h in enumerate(header) if h == "ORG"]
    org_i = org_cols[-1] if org_cols else None   # second ORG column is the abbreviation
    out = {}
    for r in rows[1:]:
        if r and r[0].strip().isdigit():
            out[int(r[0])] = r[org_i].strip() if org_i is not None and org_i < len(r) else ""
    return out


def sync_roster_status(conn: sqlite3.Connection, player_list: bytes,
                       free_agents: bytes | None = None) -> dict:
    listed = _ids_and_orgs(player_list)
    fa_ids = set(_ids_and_orgs(free_agents)) if free_agents else set()
    if len(listed) < 500:
        return {"skipped": f"player list too small ({len(listed)} rows)"}

    db = {pid: org for pid, org in conn.execute(
        "SELECT player_id, COALESCE(NULLIF(parent_team_id, 0), team_id) FROM players "
        "WHERE COALESCE(retired, 0) = 0 AND COALESCE(NULLIF(parent_team_id, 0), team_id) > 0")}

    # Which DB organization each export ORG abbreviation is (majority vote).
    votes = defaultdict(Counter)
    for pid, abbr in listed.items():
        if pid in db and abbr:
            votes[abbr][db[pid]] += 1
    covered = {c.most_common(1)[0][0] for c in votes.values() if c}
    if not covered:
        return {"skipped": "no organizations matched"}

    in_org = [pid for pid, org in db.items() if org in covered]
    present = sum(1 for pid in in_org if pid in listed)
    if in_org and present / len(in_org) < 0.8:
        return {"skipped": f"export covers only {present}/{len(in_org)} org players"}

    released, retired = [], []
    for pid in in_org:
        if pid in listed:
            continue
        (released if pid in fa_ids else retired).append(pid)

    for pid in released:
        conn.execute("UPDATE players SET team_id=0, parent_team_id=0, free_agent=1 WHERE player_id=?", (pid,))
    for pid in retired:
        conn.execute("UPDATE players SET team_id=0, parent_team_id=0, retired=1 WHERE player_id=?", (pid,))
    conn.commit()
    return {"orgs": len(covered), "released": len(released), "retired": len(retired)}
