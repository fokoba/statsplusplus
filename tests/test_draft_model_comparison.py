"""Independent draft rounds, signed differences and freshness safeguards."""
import json
import sqlite3
from types import SimpleNamespace
import pytest
import draft_model_comparison as comparison


def test_round_boundaries_and_difference_direction():
    rows=[{'pid':1,'rank':1},{'pid':2,'rank':16},{'pid':3,'rank':17},{'pid':4,'rank':49}]
    comparison.annotate_rounds(rows,16,{'players':{'1':{'rank':33,'round':3},'2':{'rank':1,'round':1},'3':{'rank':1,'round':1}}})
    assert [(r['model_round'],r['round_diff']) for r in rows]==[(1,2),(1,0),(2,-1),(4,None)]
    assert rows[0]['falsone_rank']==33
    assert rows[-1]['falsone_round'] is None


def test_filtering_does_not_rebase_the_round():
    rows=[{'pid':7,'rank':49,'adp':{'exp_round':2}}]
    comparison.annotate_rounds(rows,16,{'players':{'7':{'rank':17,'round':2}}})
    assert rows[0]['model_round']==4 and rows[0]['round_diff']==-2
    assert rows[0]['adp']=={'exp_round':2}  # market forecast remains independent


def make_input(tmp_path):
    c=sqlite3.connect(':memory:')
    for table,columns in [('players','player_id,age'),('latest_ratings','player_id,cntct'),
        ('batting_stats','player_id,year,team_id,split_id,pa'),
        ('pitching_stats','player_id,year,team_id,split_id,outs'),
        ('contracts','player_id,salary'),('teams','team_id,name')]:
        c.execute(f'CREATE TABLE {table} ({columns})')
    c.execute('INSERT INTO players VALUES (1,18)')
    c.execute('INSERT INTO latest_ratings VALUES (1,40)')
    (tmp_path/'config').mkdir()
    (tmp_path/'config/state.json').write_text(json.dumps({'game_date':'1956-03-13','year':1956,'my_team_id':6}))
    return c


def test_signature_covers_ratings_stats_calibration_and_game_date(tmp_path):
    c=make_input(tmp_path);source={'sha':'abc'}
    def sig():return comparison.input_signature(c,tmp_path,[1,2],source)
    previous=sig()
    assert previous==comparison.input_signature(c,tmp_path,[2,1],source)
    c.execute('UPDATE latest_ratings SET cntct=45')
    assert sig()!=previous;previous=sig()
    c.execute('INSERT INTO batting_stats VALUES(1,1955,6,1,100)')
    assert sig()!=previous;previous=sig()
    (tmp_path/'config/model_weights.json').write_text('{"updated":true}')
    assert sig()!=previous;previous=sig()
    (tmp_path/'config/state.json').write_text('{"game_date":"1956-04-01","year":1956,"my_team_id":6}')
    assert sig()!=previous
    assert sig()!=comparison.input_signature(c,tmp_path,[1,2],{'sha':'different'})


def source_root(tmp_path,monkeypatch):
    root=tmp_path/'source';root.mkdir()
    (root/'current.json').write_text('{"sha":"abc","version":"1.25.0"}')
    monkeypatch.setattr(comparison,'SOURCE_ROOT',root)
    monkeypatch.setattr(comparison,'input_signature',lambda *args:'fresh')
    return root


def test_fresh_cache_is_reused(tmp_path,monkeypatch):
    source_root(tmp_path,monkeypatch);(tmp_path/'config').mkdir()
    (tmp_path/'config/falsone_draft_comparison.json').write_text(json.dumps({'signature':'fresh','players':{'1':{'rank':1,'round':1}}}))
    result=comparison.load_comparison(None,tmp_path,[1])
    assert result['players']['1']['rank']==1 and not result.get('pending')


def test_stale_ranks_are_hidden_and_only_one_refresh_starts(tmp_path,monkeypatch):
    source_root(tmp_path,monkeypatch);(tmp_path/'config').mkdir()
    (tmp_path/'config/falsone_draft_comparison.json').write_text('{"signature":"old","players":{"1":{"round":99}}}')
    calls=[]
    class Thread:
        def __init__(self,**kw):calls.append(kw)
        def start(self):pass
    monkeypatch.setattr(comparison.threading,'Thread',Thread)
    monkeypatch.setattr(comparison,'_jobs',set())
    first=comparison.load_comparison(None,tmp_path,[1])
    second=comparison.load_comparison(None,tmp_path,[1])
    assert first['pending'] and first['players']=={} and second['players']=={}
    assert len(calls)==1


def test_recent_failure_is_explicit_not_fake_data(tmp_path,monkeypatch):
    source_root(tmp_path,monkeypatch);(tmp_path/'config').mkdir()
    (tmp_path/'config/falsone_draft_error.json').write_text(json.dumps({'signature':'fresh','time':comparison.time.time()}))
    result=comparison.load_comparison(None,tmp_path,[1])
    assert result['unavailable'] and result['players']=={} and not result.get('pending')


def test_missing_source_does_not_masquerade_as_custom_scores(tmp_path,monkeypatch):
    monkeypatch.setattr(comparison,'SOURCE_ROOT',tmp_path/'missing')
    assert comparison.load_comparison(None,tmp_path,[1])=={'players':{},'unavailable':True}


@pytest.mark.parametrize('payload',[{}, {'pids':[]},{'pids':['1']},{'pids':[True]},{'pids':[-1]}])
def test_invalid_poll_requests_are_rejected(payload):
    from flask import Flask
    from api_routes import api_bp
    app=Flask(__name__);app.register_blueprint(api_bp)
    r=app.test_client().post('/api/draft-model-comparison',json=payload)
    assert r.status_code==400
