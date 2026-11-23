"""Pinned, independent upstream rankings; never reuse custom derived grades."""
from pathlib import Path
from hashlib import sha256
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / 'data/upstream/falsone'
CONFIG_FILES = ('league_settings','model_weights','tool_weights','league_averages','carrying_tool_config')
_jobs = set()
_lock = threading.Lock()
log = logging.getLogger(__name__)


def input_signature(conn, league_dir, ids, source):
    """Include scouting, stats, affiliation, contracts and shared PPL calibration."""
    h = sha256(json.dumps([source['sha'], sorted(ids)]).encode())
    for table, order in [('players','player_id'),('latest_ratings','player_id'),
                         ('batting_stats','player_id,year,team_id,split_id'),
                         ('pitching_stats','player_id,year,team_id,split_id'),('contracts','player_id'),('teams','team_id')]:
        h.update(table.encode())
        for row in conn.execute(f'SELECT * FROM {table} ORDER BY {order}'):
            h.update(json.dumps(tuple(row),separators=(',',':')).encode())
    for name in CONFIG_FILES:
        p = Path(league_dir)/'config'/f'{name}.json'
        h.update(name.encode());h.update(p.read_bytes() if p.exists() else b'absent')
    state_path=Path(league_dir)/'config/state.json'
    if state_path.exists():
        state=json.loads(state_path.read_text())
        h.update(json.dumps({k:state.get(k) for k in ('game_date','year','my_team_id')},sort_keys=True).encode())
    return h.hexdigest()


def build_comparison(league_dir, ids, source=None):
    """Read-only copy of live inputs; all upstream writes stay in a temp dir."""
    league_dir = Path(league_dir)
    source = source or json.loads((SOURCE_ROOT/'current.json').read_text())
    with tempfile.TemporaryDirectory(prefix='falsone-draft-') as tmp:
        scratch = Path(tmp)/'ppl';(scratch/'config').mkdir(parents=True)
        live = sqlite3.connect(f'file:{league_dir/"league.db"}?mode=ro',uri=True)
        copied = sqlite3.connect(scratch/'league.db');live.backup(copied);live.close()
        signature = input_signature(copied,league_dir,ids,source)
        for name in CONFIG_FILES:
            path=league_dir/'config'/f'{name}.json'
            if path.exists():(scratch/'config'/path.name).write_bytes(path.read_bytes())
        state=json.loads((league_dir/'config/state.json').read_text())
        (scratch/'config/state.json').write_text(json.dumps({k:state[k] for k in ('game_date','year','my_team_id') if k in state}))
        (scratch/'config/draft_pool.json').write_text(json.dumps({'player_ids':ids}))
        # Upstream owns its derived schema; our custom DEV layout is irrelevant.
        copied.execute('DROP TABLE IF EXISTS dev_speed');copied.commit();copied.close()
        result=Path(tmp)/'result.json'
        env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','STATSPP_LEAGUE')}
        proc=subprocess.run([sys.executable,'-I',str(ROOT/'scripts/falsone_draft_runner.py'),
            str(SOURCE_ROOT/source['sha']),str(scratch),str(result)],cwd=SOURCE_ROOT/source['sha'],
            env=env,capture_output=True,text=True,timeout=600)
        if proc.returncode:
            raise RuntimeError('Independent upstream calculation failed: '+proc.stderr[-1800:])
        payload=json.loads(result.read_text());payload.update(signature=signature,source=source)
        dest=league_dir/'config/falsone_draft_comparison.json'
        temp=dest.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
        temp.write_text(json.dumps(payload));os.replace(temp,dest)
        (league_dir/'config/falsone_draft_error.json').unlink(missing_ok=True)
        return payload


def _background_build(league_dir, ids, source):
    try:build_comparison(league_dir,ids,source)
    except Exception:
        log.exception('Falsone draft comparison could not refresh')
        try:
            conn=sqlite3.connect(f'file:{Path(league_dir)/"league.db"}?mode=ro',uri=True)
            signature=input_signature(conn,league_dir,ids,source);conn.close()
            (Path(league_dir)/'config/falsone_draft_error.json').write_text(
                json.dumps({'signature':signature,'time':time.time()}))
        except Exception:log.exception('Could not record comparison failure')
    finally:
        with _lock:_jobs.discard(str(league_dir))


def load_comparison(conn, league_dir, ids):
    """Never display stale ranks; refresh changed inputs without blocking the page."""
    league_dir=Path(league_dir)
    try:
        source=json.loads((SOURCE_ROOT/'current.json').read_text())
        signature=input_signature(conn,league_dir,ids,source)
        path=league_dir/'config/falsone_draft_comparison.json'
        payload=json.loads(path.read_text()) if path.exists() else {}
        if payload.get('signature')==signature:
            return payload
        error_path=league_dir/'config/falsone_draft_error.json'
        if error_path.exists():
            error=json.loads(error_path.read_text())
            if error.get('signature')==signature and time.time()-error.get('time',0)<120:
                return {'players':{},'source':source,'unavailable':True}
        key=str(league_dir)
        with _lock:
            if key not in _jobs:
                _jobs.add(key)
                threading.Thread(target=_background_build,args=(league_dir,ids,source),daemon=True).start()
        return {'players':{},'source':source,'pending':True}
    except (OSError,ValueError,sqlite3.Error):
        log.exception('Falsone comparison unavailable')
        return {'players':{},'unavailable':True}


def annotate_rounds(results, teams, comparison):
    """Positive difference = our board values the player earlier (higher)."""
    upstream=comparison.get('players',{})
    for r in results:
        mine=(r['rank']-1)//teams+1
        other=upstream.get(str(r['pid']),{})
        r['model_round']=mine
        r['falsone_rank']=other.get('rank')
        r['falsone_round']=other.get('round')
        r['round_diff']=other['round']-mine if other.get('round') is not None else None
