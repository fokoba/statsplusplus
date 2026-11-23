"""Run pinned upstream scoring in a separate interpreter and scratch league.

Invoked with python -I; no imports from the custom fork may enter this process.
"""
from pathlib import Path
import json
import sys

source, league, output = map(Path, sys.argv[1:4])
sys.path[:0] = [str(source / 'src'), str(source / 'scripts')]
from statsplusplus.data import db, evaluation_engine, fv_calc
import draft_board

assert Path(evaluation_engine.__file__).is_relative_to(source)
db.init_schema(league)
evaluation_engine.run(league_dir=league)
fv_calc.run(league_dir=league)
rows, adp, _, teams, conn = draft_board.load_board(league)
ids = set(json.loads((league / 'config/draft_pool.json').read_text())['player_ids'])
assert ids == {r['player_id'] for r in rows}, 'Upstream did not score the whole pool'
players = {str(r['player_id']): {'rank': adp[r['player_id']]['fv_rank'],
    'round': (adp[r['player_id']]['fv_rank']-1)//teams+1,
    'score': draft_board.draft_value(r), 'fv':r['fv']} for r in rows}
assert sorted(p['rank'] for p in players.values()) == list(range(1,len(ids)+1))
output.write_text(json.dumps({'players':players,'teams':teams}))
conn.close()
