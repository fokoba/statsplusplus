#!/usr/bin/env python3
"""fv_calc.py — League-wide FV and surplus calculation.

This is a thin shim that delegates to the package implementation.
"""

import logging
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

from statsplusplus.data.fv_calc import run, RATINGS_SQL

logger = logging.getLogger(__name__)


def _check_fv_tier_discrepancy(p, fv_base, fv_risk):
    """Log a warning when the component-based defensive bonus produces an FV
    grade differing from the old defensive_score() path by more than one FV
    tier (5 FV points).  Only runs when ``_defensive_value`` was used."""
    if p.get("_defensive_value") is None:
        return
    from player_utils import calc_fv
    p_old = dict(p)
    del p_old["_defensive_value"]
    fv_old, _ = calc_fv(p_old)

    if abs(fv_base - fv_old) > 5:
        logger.warning(
            "FV tier discrepancy for player %s: component-based=%d, "
            "raw-tool-based=%d (defensive_value=%s)",
            p.get("ID", "?"), fv_base, fv_old, p["_defensive_value"],
        )


if __name__ == "__main__":
    run()
