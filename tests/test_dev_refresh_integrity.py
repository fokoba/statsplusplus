"""Same-date history, newest reports and import ordering."""
import sqlite3
from statsplusplus.data.ratings_history import snapshot_current_ratings
from statsplusplus.data.db import SCHEMA
from statsplusplus.data.fv_calc import _compute_dev_speed_pass


def connection():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;c.executescript(SCHEMA)
    c.execute('ALTER TABLE players ADD COLUMN draft_year INTEGER')
    return c


def test_history_never_backdates_current_scores():
    c=connection()
    c.execute("INSERT INTO ratings_history(player_id,snapshot_date,cntct,composite_score) VALUES(1,'1956-03-06',40,40)")
    c.execute("INSERT INTO ratings(player_id,snapshot_date,cntct,composite_score) VALUES(1,'1956-03-13',45,43)")
    snapshot_current_ratings(c)
    assert [tuple(r) for r in c.execute('SELECT snapshot_date,cntct,composite_score FROM ratings_history ORDER BY snapshot_date')]==[('1956-03-06',40,40),('1956-03-13',45,43)]
    c.execute('UPDATE ratings SET cntct=50,composite_score=46');snapshot_current_ratings(c)
    assert c.execute('SELECT count(*) FROM ratings_history').fetchone()[0]==2
    assert tuple(c.execute("SELECT cntct,composite_score FROM ratings_history WHERE snapshot_date='1956-03-13'").fetchone())==(50,46)
    assert c.execute("SELECT composite_score FROM ratings_history WHERE snapshot_date='1956-03-06'").fetchone()[0]==40


def test_latest_report_included_future_history_excluded():
    c=connection();c.execute('INSERT INTO players(player_id,age,draft_year) VALUES(1,22,1953)')
    for d in ['1955-03-13','1955-05-13','1955-07-13','1955-09-13','1955-11-13','1956-01-13','1956-03-06','1956-04-01']:
        v=75 if d>'1956-03-13' else 40
        c.execute('INSERT INTO ratings_history(player_id,snapshot_date,cntct,gap,pow,eye,pot_cntct,pot_gap,pot_pow,pot_eye) VALUES(1,?,?,?,?,?,60,60,60,60)',(d,v,v,v,v))
    c.execute("INSERT INTO ratings(player_id,snapshot_date,cntct,gap,pow,eye,pot_cntct,pot_gap,pot_pow,pot_eye,acc) VALUES(1,'1956-03-13',45,45,45,45,60,60,60,60,'VH')")
    _compute_dev_speed_pass(c,'1956-03-13',{1:('COF',22)})
    r=dict(c.execute('SELECT * FROM dev_speed').fetchone())
    assert r['comp_last']==45 and r['comp_first']==40
    assert '1956-03-13' in r['note'] and 'Contact +5' in r['note']
    assert '1956-04-01' not in r['note'] and r['label']=='Gains observed'


def test_import_refresh_happens_after_raw_commit_and_close(monkeypatch):
    import custom_upload
    import statsplusplus.data.derived_refresh as derived
    calls=[]
    class Fake:
        def execute(self,*args):return self
        def fetchone(self):return ('1956-03-13',)
        def commit(self):calls.append('commit')
        def close(self):calls.append('close')
    monkeypatch.setattr(custom_upload,'get_conn',lambda *a:Fake())
    monkeypatch.setattr(custom_upload,'parse_rows',lambda *a:[])
    monkeypatch.setattr(derived,'refresh_derived_ratings',lambda *a:calls.append('derived'))
    custom_upload.import_ratings_sync(b'',league_dir='/tmp/test-only')
    assert calls==['commit','close','derived']


def test_compact_display_has_no_legacy_warning_language():
    from web_league_context import dev_cell
    old=(1,'stalled','Stalled — upside at risk','High',-1.8,'at_risk','At Risk','real chance this does not come together')
    d=dev_cell(old,0)
    assert d['label']=='Limited evidence' and d['schedule_badge_text']=='Limited evidence'
    assert d['css_class']=='none'
    early=dev_cell((0,'none','Early view','Low',None,'early','Early view','More history needed'),0)
    assert early['schedule_badge_text']=='Early view' and early['dim']


def test_many_daily_reports_do_not_fake_longitudinal_confidence():
    from statsplusplus.evaluation import dev_speed as ds
    from datetime import date,timedelta
    rows=[]
    for d in ['1955-01-01']+[(date(1956,1,1)+timedelta(days=i)).isoformat() for i in range(10)]:
        rows.append(dict(snapshot_date=d,cntct=40,gap=40,pow=40,eye=40,pot_cntct=60,pot_gap=60,pot_pow=60,pot_eye=60))
    r=ds.compute_dev_speed(bucket='COF',age=22,window=rows,baseline={'off':{('HIT','ALL'):(0,1,100)}},acc='VH')
    assert r['confidence']=='Low' and r['label']=='Limited evidence'
