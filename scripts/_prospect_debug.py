"""Debug prospect evaluation: Omari Pauldo and defense-heavy prospects."""
import sys, sqlite3, json
sys.path.insert(0, "scripts")

# Ensure EMLB
with open("data/app_config.json", "w") as f:
    json.dump({"statsplus_cookie": "sessionid=5tm3hnjwld3biah7c9uhh7g3yajz9xs8", "active_league": "emlb"}, f, indent=2)

from statsplusplus.config.league_context import get_league_dir, get_active_league_slug
from statsplusplus.config.league_config import LeagueConfig
from statsplusplus.config.ratings import norm as _norm_pkg
from statsplusplus.utils.positions import assign_bucket
from statsplusplus.data.evaluation_engine import (
    compute_composite_hitter, compute_ceiling, load_tool_weights,
    _extract_hitter_tools, _extract_potential_hitter_tools,
    _extract_defense_tools, _get_def_weights_for_bucket,
    DEFAULT_TOOL_WEIGHTS, _KEY_MAP,
)

league_dir = get_league_dir(get_active_league_slug())
_cfg = LeagueConfig(base_dir=league_dir)
def norm(val):
    return _norm_pkg(val, _cfg.ratings_scale)
conn = sqlite3.connect(str(league_dir / "league.db"))
conn.row_factory = sqlite3.Row
snap = conn.execute("SELECT MAX(snapshot_date) as sd FROM ratings").fetchone()["sd"]

pauldo = conn.execute("""
    SELECT r.*, p.name, p.age, p.pos, p.role, p.level, p.team_id,
           pf.fv
    FROM ratings r
    JOIN players p ON r.player_id = p.player_id
    LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
    WHERE r.snapshot_date = ? AND p.name LIKE '%Pauldo%'
""", (snap,)).fetchone()

print("=" * 70)
print(f"PLAYER: {pauldo['name']} (age {pauldo['age']}, level {pauldo['level']}, pos {pauldo['pos']})")
print("=" * 70)

print(f"\n--- Scores ---")
print(f"  Composite:  {pauldo['composite_score']}")
print(f"  Ceiling:    {pauldo['ceiling_score']}")
print(f"  Tool Only:  {pauldo['tool_only_score']}")
print(f"  OVR:        {norm(pauldo['ovr'])}")
print(f"  POT:        {norm(pauldo['pot'])}")
print(f"  FV:         {pauldo['fv']}")

print(f"\n--- Hitting Tools (current / potential) ---")
for tool, cur_col, pot_col in [
    ("Contact", "cntct", "pot_cntct"), ("Gap", "gap", "pot_gap"),
    ("Power", "pow", "pot_pow"), ("Eye", "eye", "pot_eye"),
    ("Avoid K", "ks", "pot_ks"), ("Speed", "speed", None),
    ("Steal", "steal", None), ("Stl Rate", "stl_rt", None),
]:
    cur = norm(pauldo[cur_col]) if pauldo.get(cur_col) else None
    pot = norm(pauldo[pot_col]) if pot_col and pauldo.get(pot_col) else None
    if pot:
        print(f"  {tool:<12} {cur:>3} / {pot:>3}")
    else:
        print(f"  {tool:<12} {cur if cur else 'N/A':>3}")

print(f"\n--- Defense Tools ---")
for tool, col in [("OF Range", "ofr"), ("OF Error", "ofe"), ("OF Arm", "ofa"),
                   ("CF pos", "cf"), ("LF pos", "lf"), ("RF pos", "rf")]:
    val = pauldo.get(col)
    if val and val > 0:
        print(f"  {tool:<12} {norm(val):>3}")

# Bucket and weights
row_dict = dict(pauldo)
p = dict(pauldo)
p["Pos"] = str(p.get("pos") or "")
role_map = {11: "starter", 12: "reliever", 13: "closer"}
p["_role"] = role_map.get(p.get("role") or 0, "")
p["Age"] = p.get("age", 99)
for db_key, api_key in _KEY_MAP.items():
    if db_key in p:
        v = p[db_key]
        if isinstance(v, (int, float)):
            p[api_key] = v
        elif v is not None and str(v).lstrip("-").isdigit():
            p[api_key] = int(v)
        else:
            p[api_key] = 0

bucket = assign_bucket(p, use_pot=False)
print(f"\n--- Composite Trace (bucket={bucket}) ---")

tw = load_tool_weights(league_dir)
hitter_weights = tw.get("hitter", DEFAULT_TOOL_WEIGHTS["hitter"])
h_weights = hitter_weights.get(bucket, hitter_weights.get("COF", {}))

print(f"\n  Weights:")
for k, v in sorted(h_weights.items(), key=lambda x: -x[1]):
    print(f"    {k:<12} {v:.3f}")

hitter_tools = _extract_hitter_tools(row_dict, norm)
defense_tools = _extract_defense_tools(row_dict)
def_weights = _get_def_weights_for_bucket(bucket)

# Manual composite breakdown
defense_weight = h_weights.get("defense", 0.0)
offensive_share = 1.0 - defense_weight

print(f"\n  Offensive tools (share={offensive_share:.2f}):")
off_total_w = 0
off_sum = 0
for k in ("contact", "gap", "power", "eye", "speed", "steal", "stl_rt"):
    val = hitter_tools.get(k)
    w = h_weights.get(k, 0)
    if val is not None and w > 0:
        off_total_w += w
        print(f"    {k:<12} val={val:>3}  raw_w={w:.3f}")

print(f"  Total offensive weight: {off_total_w:.3f}")
print(f"  Renormalized to fill offensive share ({offensive_share:.2f}):")
for k in ("contact", "gap", "power", "eye", "speed", "steal", "stl_rt"):
    val = hitter_tools.get(k)
    w = h_weights.get(k, 0)
    if val is not None and w > 0:
        effective_w = (w / off_total_w) * offensive_share
        contribution = val * effective_w
        print(f"    {k:<12} val={val:>3} × eff_w={effective_w:.3f} = {contribution:.1f}")
        off_sum += contribution

print(f"  Offensive sum: {off_sum:.1f}")

# Defense
def_score = 0
print(f"\n  Defense (weight={defense_weight:.3f}):")
for dk, dw in def_weights.items():
    dv = defense_tools.get(dk)
    if dv is not None:
        nv = norm(dv) or 0
        contribution = nv * dw
        def_score += contribution
        if dw > 0:
            print(f"    {dk:<12} val={nv:>3} × dw={dw:.3f} = {contribution:.1f}")
print(f"  Defensive composite: {def_score:.1f}")
print(f"  Defense contribution: {def_score * defense_weight:.1f}")

raw = off_sum + def_score * defense_weight
print(f"\n  Raw (off + def): {raw:.1f}")

# Elite bonus
elite_bonus = 0
for k in ("contact", "gap", "power", "eye", "speed", "steal", "stl_rt"):
    val = hitter_tools.get(k)
    w = h_weights.get(k, 0)
    if val is not None and w > 0 and val >= 60:
        bonus = (val - 60) * 0.5 * (w / off_total_w) * offensive_share
        elite_bonus += bonus
        print(f"  Elite bonus: {k}={val} → +{bonus:.2f}")
print(f"  Total elite bonus: {elite_bonus:.1f}")
print(f"  Final raw: {raw + elite_bonus:.1f} → clamped: {max(20, min(80, round(raw + elite_bonus)))}")

score = compute_composite_hitter(hitter_tools, h_weights, defense_tools, def_weights)
print(f"  compute_composite_hitter result: {score}")

# Ceiling trace
pot_tools = _extract_potential_hitter_tools(row_dict, norm)
print(f"\n--- Ceiling Trace ---")
print(f"  Potential tools:")
for k, v in pot_tools.items():
    print(f"    {k:<12} {v}")
print(f"  Age: {pauldo['age']}, POT: {pauldo['pot']}")
print(f"  Acc: {pauldo.get('acc', 'A')}, WrkEthic: {pauldo.get('wrk_ethic', 'N')}")

ceiling = compute_ceiling(
    pot_tools, h_weights, score,
    accuracy=row_dict.get("acc") or "A",
    work_ethic=row_dict.get("wrk_ethic") or "N",
    defense=defense_tools, def_weights=def_weights,
    age=pauldo["age"], pot=pauldo["pot"],
)
print(f"  Computed ceiling: {ceiling}")

# Now show other CF prospects for comparison
print("\n" + "=" * 70)
print("CF PROSPECTS WITH FV >= 45 (sorted by FV desc)")
print("=" * 70)
prospects = conn.execute("""
    SELECT r.composite_score, r.ceiling_score, r.ovr, r.pot,
           r.cntct, r.pot_cntct, r.gap, r.pot_gap, r.pow, r.pot_pow,
           r.eye, r.pot_eye, r.speed, r.ofr,
           p.name, p.age, p.level,
           pf.fv
    FROM ratings r
    JOIN players p ON r.player_id = p.player_id
    LEFT JOIN prospect_fv pf ON p.player_id = pf.player_id
    WHERE r.snapshot_date = ? AND r.composite_score IS NOT NULL
      AND p.pos = '8' AND pf.fv >= 45
    ORDER BY pf.fv DESC, r.composite_score DESC
    LIMIT 20
""", (snap,)).fetchall()

print(f"\n{'Name':<22} {'Age':>3} {'Lvl':>3} {'FV':>3} {'Comp':>4} {'Ceil':>4} {'OVR':>4} {'POT':>4} {'Cnt':>4} {'PtCnt':>5} {'Pow':>4} {'PtPow':>5} {'Spd':>4} {'OFR':>4}")
print("-" * 100)
for r in prospects:
    print(f"{r['name']:<22} {r['age']:>3} {r['level']:>3} {r['fv'] or 0:>3} {r['composite_score']:>4} {r['ceiling_score']:>4} {norm(r['ovr']):>4} {norm(r['pot']):>4} {norm(r['cntct']):>4} {norm(r['pot_cntct']):>5} {norm(r['pow']):>4} {norm(r['pot_pow']):>5} {norm(r['speed']):>4} {norm(r['ofr']):>4}")

conn.close()
