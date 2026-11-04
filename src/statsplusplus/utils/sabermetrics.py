"""Shared sabermetric stat formulas.

Centralizes derived-stat formulas that were previously duplicated across the
web query modules so there is a single source of truth. Currently holds SIERA;
FIP/BABIP/etc. can migrate here over time.
"""

from __future__ import annotations

# ── SIERA ─────────────────────────────────────────────────────────────────
# Skill-Interactive ERA, per the FanGraphs/Baseball-Prospectus regression
# (Swartz 2011, "New SIERA"). The estimator regresses ERA on strikeout rate,
# walk rate, and *net* ground-ball rate (ground balls minus air balls), with
# squared and interaction terms so the three skills combine nonlinearly.
#
#   SIERA = const
#           - 15.518 * (SO/PA)
#           +  9.146 * (SO/PA)^2
#           +  8.648 * (BB/PA)
#           + 27.252 * (BB/PA)^2
#           -  2.298 * (netGB/PA)
#           +/- 4.920 * (netGB/PA)^2      (sign depends on netGB sign)
#           -  4.036 * (SO/PA) * (BB/PA)
#           +  5.155 * (SO/PA) * (netGB/PA)
#           +  4.546 * (BB/PA) * (netGB/PA)
#
# netGB = GB - (FB + PU). OOTP/StatsPlus reports batted balls in two buckets
# only -- GB and FB -- where FB is the air-ball bucket (fly balls including
# pop-ups), so netGB = gb - fb is the correct net ground-ball count and no
# pop-up field is being dropped.
#
# The netGB^2 term's coefficient flips sign with the sign of netGB: the
# published formula writes it as "-/+ 4.920", i.e. the quadratic penalty is
# -4.920 for ground-ball pitchers (netGB > 0) and +4.920 for fly-ball
# pitchers (netGB < 0). This makes the curve concave on both sides of zero.

SIERA_CONST = 6.145

_C_SO = -15.518
_C_SO2 = 9.146
_C_BB = 8.648
_C_BB2 = 27.252
_C_NGB = -2.298
_C_NGB2 = 4.920  # magnitude; sign set per netGB sign below
_C_SO_BB = -4.036
_C_SO_NGB = 5.155
_C_BB_NGB = 4.546


def siera(k, bb, gb, fb, bf, const=SIERA_CONST):
    """Canonical FanGraphs SIERA.

    Args:
        k:   strikeouts
        bb:  walks (unintentional + intentional, as reported)
        gb:  ground balls
        fb:  air balls (fly balls incl. pop-ups) -- the StatsPlus FB bucket
        bf:  batters faced (plate appearances)
        const: run-environment constant (FanGraphs tunes this yearly).

    Returns ``None`` when SIERA is undefined (no batters faced, or no
    batted-ball data to form netGB). Callers decide how to display ``None``
    (e.g. "-" for MiLB rows without batted-ball data).
    """
    if not bf:
        return None
    # netGB requires batted-ball data; without gb/fb we cannot form it.
    if (gb or 0) == 0 and (fb or 0) == 0:
        return None

    so_pa = (k or 0) / bf
    bb_pa = (bb or 0) / bf
    ngb_pa = ((gb or 0) - (fb or 0)) / bf

    ngb2_coef = _C_NGB2 if ngb_pa < 0 else -_C_NGB2

    return (
        const
        + _C_SO * so_pa
        + _C_SO2 * so_pa ** 2
        + _C_BB * bb_pa
        + _C_BB2 * bb_pa ** 2
        + _C_NGB * ngb_pa
        + ngb2_coef * ngb_pa ** 2
        + _C_SO_BB * so_pa * bb_pa
        + _C_SO_NGB * so_pa * ngb_pa
        + _C_BB_NGB * bb_pa * ngb_pa
    )
