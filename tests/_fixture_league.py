"""Shared on-disk fixture-league builder for smoke tests.

Builds a minimal but complete league under ``data/<slug>/`` (config JSONs +
seeded SQLite DB) so subprocess CLI tools and the live Flask app can be
exercised against it. Two variants:

  - ``with_ovr=True``  — ovr/pot populated (normal OOTP league)
  - ``with_ovr=False`` — ovr/pot NULL, composite/ceiling populated (PPL-style
    league that doesn't surface OVR/POT)

Fixtures live under the real ``data/`` dir because league context resolves
relative to the project root; callers are responsible for cleanup (see
``build_fixture`` / ``remove_fixture``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from statsplusplus.data.db import get_connection, init_schema

BASE = Path(__file__).resolve().parent.parent
DATA_DIR = BASE / "data"

YEAR = 2033
EVAL_DATE = "2033-04-01"
SNAP = "2033-04-01"
TEAM_ID = 1
# Well-known fixture entity IDs for tests to assert against.
MLB_HITTER_ID = 100
MLB_SP_ID = 101
PROSPECT_ID = 103


def _write_config(league_dir: Path) -> None:
    cfg = league_dir / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "league_settings.json").write_text(json.dumps({
        "league": "Smoke Test League",
        "statsplus_slug": "smoke",
        "ratings_scale": "20-80",
        "minimum_salary": 720000,
        "default_team_id": TEAM_ID,
        "teams": {str(TEAM_ID): {"name": "Test Team", "abbr": "TST"},
                  "2": {"name": "Rival", "abbr": "RIV"}},
        "leagues": [{"name": "TL", "short": "TL", "color": "#50b4ff",
                     "divisions": {"East": [TEAM_ID, 2]}}],
        "wild_cards_per_league": 1,
    }))
    (cfg / "state.json").write_text(json.dumps({
        "game_date": "2033-07-01", "year": YEAR, "my_team_id": TEAM_ID,
    }))
    (cfg / "league_averages.json").write_text(json.dumps({
        "dollar_per_war": 8000000,
        "batting": {"avg": 0.260, "obp": 0.330, "slg": 0.420, "ops": 0.750,
                    "iso": 0.160, "babip": 0.295, "k_pct": 22.0, "bb_pct": 8.5,
                    "hr": 22, "sb": 10, "woba": 0.320},
        "pitching": {"era": 4.00, "fip": 4.05, "k_pct": 22.0, "bb_pct": 8.0,
                     "babip": 0.295, "whip": 1.28, "k9": 8.5, "bb9": 3.0},
    }))


def _seed(conn, *, with_ovr: bool) -> None:
    ovr_h = 55 if with_ovr else None
    pot_h = 60 if with_ovr else None
    ovr_p = 58 if with_ovr else None
    pot_p = 63 if with_ovr else None

    def _ins(table, **cols):
        keys = ",".join(cols)
        conn.execute(f"INSERT OR REPLACE INTO {table} ({keys}) "
                     f"VALUES ({','.join('?' * len(cols))})", list(cols.values()))

    _ins("teams", team_id=TEAM_ID, name="Test Team", level="1", parent_team_id=0, league="TL")
    _ins("teams", team_id=2, name="Rival", level="1", parent_team_id=0, league="TL")

    players = [
        (100, "Joe Hitter", 27, TEAM_ID, 0, "1", 8, 0),         # MLB CF
        (101, "Sam Starter", 25, TEAM_ID, 0, "1", 1, 11),       # MLB SP
        (102, "Rick Reliever", 28, TEAM_ID, 0, "1", 1, 13),     # MLB RP
        (103, "Bob Prospect", 21, TEAM_ID, TEAM_ID, "3", 6, 0),  # AA SS prospect
        (200, "Rival Bat", 29, 2, 0, "1", 8, 0),
        (201, "Rival Arm", 30, 2, 0, "1", 1, 11),
    ]
    for pid, name, age, tid, ptid, lvl, pos, role in players:
        _ins("players", player_id=pid, name=name, age=age, team_id=tid,
             parent_team_id=ptid, level=lvl, pos=pos, role=role)

    def _rating(pid, ovr, pot, comp, ceil):
        _ins("ratings", player_id=pid, snapshot_date=SNAP, ovr=ovr, pot=pot,
             composite_score=comp, ceiling_score=ceil, league_id=1,
             cntct=55, gap=50, pow=52, eye=54, ks=50, speed=55, steal=50,
             pot_cntct=57, pot_gap=52, pot_pow=55, pot_eye=56, pot_ks=52,
             c=20, ss=55, second_b=50, third_b=45, first_b=40, lf=45, cf=55, rf=45,
             ofr=55, ofa=50, ofe=50, ifr=45, ifa=45, ife=45, tdp=45,
             stf=60, mov=55, ctrl=52, ctrl_r=52, ctrl_l=50, stm=55, vel="94",
             pot_stf=62, pot_mov=57, pot_ctrl=54,
             acc="A", wrk_ethic="N", height=185, bats="R", throws="R")

    _rating(100, ovr_h, pot_h, 56, 61)
    _rating(101, ovr_p, pot_p, 59, 64)
    _rating(102, ovr_p, pot_p, 52, 55)
    _rating(103, (48 if with_ovr else None), (60 if with_ovr else None), 50, 62)
    _rating(200, ovr_h, pot_h, 54, 59)
    _rating(201, ovr_p, pot_p, 57, 62)

    # Contracts — all salary years populated (refresh always writes them).
    for pid in (100, 101, 102, 200, 201):
        _ins("contracts", player_id=pid,
             team_id=TEAM_ID if pid < 200 else 2,
             contract_team_id=TEAM_ID if pid < 200 else 2,
             is_major=1, season_year=YEAR, years=3, current_year=0,
             salary_0=5_000_000, salary_1=6_000_000, salary_2=7_000_000)

    _ins("batting_stats", player_id=100, year=YEAR, team_id=TEAM_ID, split_id=1,
         pa=400, ab=360, h=104, d=22, t=2, hr=18, r=55, rbi=60, bb=35, k=70, sb=8, g=100,
         avg=0.289, obp=0.360, slg=0.480, war=2.4)
    _ins("batting_stats", player_id=200, year=YEAR, team_id=2, split_id=1,
         pa=380, ab=350, h=91, d=18, t=1, hr=12, r=44, rbi=48, bb=25, k=80, sb=3, g=95,
         avg=0.260, obp=0.315, slg=0.410, war=1.2)
    _ins("pitching_stats", player_id=101, year=YEAR, team_id=TEAM_ID, split_id=1,
         ip=180.0, outs=540, g=30, gs=30, era=3.50, k=190, bb=50, ha=160, er=70, r=75, war=3.8)
    _ins("pitching_stats", player_id=102, year=YEAR, team_id=TEAM_ID, split_id=1,
         ip=65.0, outs=195, g=60, gs=0, era=3.10, k=75, bb=20, ha=55, er=22, r=24, war=1.1)

    _ins("team_batting_stats", team_id=TEAM_ID, year=YEAR, split_id=1, name="Test Team",
         r=720, pa=6100, ab=5500, h=1450, hr=190, bb=520, k=1200, sb=90,
         avg=0.264, obp=0.335, slg=0.430, ops=0.765, iso=0.166, k_pct=19.7, bb_pct=8.5,
         babip=0.298, woba=0.325)
    _ins("team_batting_stats", team_id=2, year=YEAR, split_id=1, name="Rival",
         r=650, pa=6000, ab=5450, h=1380, hr=160, bb=480, k=1300, sb=70,
         avg=0.253, obp=0.320, slg=0.405, ops=0.725, iso=0.152, k_pct=21.7, bb_pct=8.0,
         babip=0.290, woba=0.312)
    _ins("team_pitching_stats", team_id=TEAM_ID, year=YEAR, split_id=1, name="Test Team",
         r=640, ip=1440.0, era=4.00, fip=4.05, hra=160, bb=460, k=1250, ha=1350, er=640,
         k_pct=21.0, bb_pct=7.8)
    _ins("team_pitching_stats", team_id=2, year=YEAR, split_id=1, name="Rival",
         r=700, ip=1440.0, era=4.38, fip=4.40, hra=180, bb=500, k=1180, ha=1420, er=700,
         k_pct=19.8, bb_pct=8.4)
    conn.commit()


def build_fixture(slug: str, with_ovr: bool) -> Path:
    """Create data/<slug>/ with config + a seeded DB. Returns the league dir."""
    league_dir = DATA_DIR / slug
    _write_config(league_dir)
    (league_dir / "tmp").mkdir(parents=True, exist_ok=True)
    init_schema(league_dir)
    conn = get_connection(league_dir)
    _seed(conn, with_ovr=with_ovr)
    conn.execute(
        "INSERT OR REPLACE INTO prospect_fv "
        "(player_id, eval_date, fv, fv_str, level, bucket, prospect_surplus, risk, fv_continuous) "
        "VALUES (103, ?, 50, '50', 'AA', 'SS', 8000000, 'Medium', 50.0)",
        (EVAL_DATE,),
    )
    for pid, name, bkt, comp in ((100, "Joe Hitter", "CF", 56), (101, "Sam Starter", "SP", 59)):
        conn.execute(
            "INSERT OR REPLACE INTO player_surplus "
            "(player_id, eval_date, name, bucket, age, ovr, fv, fv_str, "
            " surplus, surplus_yr1, level, team_id, parent_team_id) VALUES "
            "(?, ?, ?, ?, 27, ?, 55, '55', 12000000, 4000000, 'MLB', ?, 0)",
            (pid, EVAL_DATE, name, bkt, comp, TEAM_ID),
        )
    conn.commit()
    conn.close()
    return league_dir


def remove_fixture(slug: str) -> None:
    shutil.rmtree(DATA_DIR / slug, ignore_errors=True)
