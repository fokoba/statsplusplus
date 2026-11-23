"""DEV reports observed changes, never unvalidated failure warnings."""
import pytest
from datetime import date,timedelta
from statsplusplus.evaluation import dev_speed as ds


def window(years=1,changes=(5,5,0,0),start=40,potential=60,snaps=7):
    return [dict(snapshot_date=(date(1955,1,1)+timedelta(days=round(365*years*i/(snaps-1)))).isoformat(),
        composite_score=40-2*i,ceiling_score=60+i,true_ceiling=80,
        **{k:start+v*i/(snaps-1) for k,v in zip(ds.HITTER_TOOLS,changes)},
        **{'pot_'+k:potential for k in ds.HITTER_TOOLS}) for i in range(snaps)]


def evaluate(rows,**kw):
    args=dict(bucket='COF',age=22,window=rows,baseline={'off':{('HIT','22-23'):(0,1,100)}},acc='VH')
    args.update(kw)
    return ds.compute_dev_speed(**args)


def test_gains_survive_flat_tools_and_composite_decline():
    r=evaluate(window())
    assert r['available'] and r['label']=='Faster gains'
    assert r['d_ovr']==2.5 and r['comp_last']==42.5
    assert r['stagnant_tools']==['pow','eye']


def test_three_declining_tools_cannot_be_broad_progress():
    r=evaluate(window(changes=(-5,-5,30,-5)))
    assert r['label']=='Mixed changes'
    assert 'Contact -5' in r['note'] and 'Power +30' in r['note']
    assert 'broadly' not in r['note']


@pytest.mark.parametrize('overrides,years,expected',[
    ({'age':17},1,'Early view'),({'rookie':True},1,'Early view'),
    ({},.5,'Early view'),({'acc':'VL'},1,'Limited evidence'),
    ({'acc':None},1,'Limited evidence')])
def test_uncertain_evidence_is_not_a_warning(overrides,years,expected):
    r=evaluate(window(years=years),**overrides)
    assert not r['available'] and r['label']==expected
    assert r['schedule_status'] not in ('at_risk','behind')


def test_short_window_closure_is_annualized():
    rows=window(years=.5,changes=(2,2,2,2));r=evaluate(rows)
    yrs=ds._years_between(rows[0]['snapshot_date'],rows[-1]['snapshot_date'])
    assert r['gap_closed_pct_yr']==pytest.approx(round(.1/yrs,3))


def test_potential_growth_is_not_failure():
    rows=window(changes=(5,5,5,5))
    for i,r in enumerate(rows):
        for k in ds.HITTER_TOOLS:r['pot_'+k]=60+i*2
    r=evaluate(rows)
    assert r['gap_trend']>0 and r['d_pot']>0 and r['label']=='Faster gains'
    assert 'not itself a setback' in r['note']


def test_three_snapshot_smoothing_is_not_zero():
    rows=[dict(snapshot_date=d,composite_score=c,ceiling_score=60) for d,c in
          [('1955-01-01',40),('1955-04-01',45),('1955-07-01',50)]]
    smooth,trend=ds.gap_smoothed_and_trend(rows)
    assert smooth==10 and trend==pytest.approx(-20.18,abs=.01)


def test_smoothed_trend_uses_mean_dates():
    rows=[dict(snapshot_date=f'1955-0{i+1}-01',composite_score=40+i,ceiling_score=60) for i in range(6)]
    assert -12.5<ds.gap_smoothed_and_trend(rows)[1]<-11.5


def test_model_or_defensive_revision_cannot_create_development():
    a=window(changes=(0,0,0,0))
    b=[dict(r,composite_score=80,ceiling_score=80,offensive_grade=80,defensive_value=80) for r in a]
    assert evaluate(a)==evaluate(b)
    assert evaluate(a)['label']=='Little change'


def test_missing_current_tools_are_not_fake_gains():
    rows=window()
    for r in rows:r['eye']=None
    assert evaluate(rows) is None


def test_missing_potential_does_not_invent_gap():
    rows=window()
    for r in rows:r['pot_eye']=None
    r=evaluate(rows)
    assert r['gap'] is None and r['gap_closed_pct_yr'] is None
    assert r['label']=='Faster gains'


def test_no_peers_does_not_imply_schedule():
    r=evaluate(window(),baseline={})
    assert not r['available'] and 'baseline is not available' in r['note']


def test_one_report_is_early_view():
    r=evaluate(window()[:1])
    assert r['label']=='Early view' and r['annual_move'] is None


def test_potential_and_current_use_same_normalization():
    rows=window(start=10,potential=15,changes=(2,2,2,2))
    r=evaluate(ds.prepare_tool_window(rows,'COF','1-20'))
    assert r['gap']==pytest.approx(9.47,abs=.01)


def test_playing_time_cannot_inflate_rating_confidence():
    assert ds.confidence_tier(1,7,'VH',0,False)==ds.confidence_tier(1,7,'VH',10000,False)=='High'
