"""Tests for statsplusplus.utils — formatting and position utilities."""

import sys
from pathlib import Path

import pytest

from statsplusplus.utils.formatting import (
    fmt_money,
    fmt_ip,
    short_name,
    height_str,
    fmt_pct,
    fmt_avg,
)
from statsplusplus.utils.positions import (
    ROLE_MAP,
    GAME_POS_MAP,
    LEVEL_KEY_MAP,
    LEVEL_DISPLAY_MAP,
    LEVEL_NORM_AGE,
    PITCH_FIELDS,
    PITCH_NAMES,
    display_pos,
    next_level,
    level_key,
    level_display,
    PositionalBucket,
)


# ---------------------------------------------------------------------------
# fmt_money
# ---------------------------------------------------------------------------

class TestFmtMoney:
    def test_millions(self):
        assert fmt_money(15_000_000) == "$15.0M"
        assert fmt_money(1_500_000) == "$1.5M"
        assert fmt_money(10_200_000) == "$10.2M"

    def test_thousands(self):
        assert fmt_money(825_000) == "$825K"
        assert fmt_money(150_000) == "$150K"
        assert fmt_money(1_000) == "$1K"

    def test_small(self):
        assert fmt_money(500) == "$500"
        assert fmt_money(0) == "$0"

    def test_negative(self):
        assert fmt_money(-5_000_000) == "$-5.0M"
        assert fmt_money(-100_000) == "$-100K"

    def test_none(self):
        assert fmt_money(None) == "—"

    def test_string_passthrough(self):
        assert fmt_money("N/A") == "N/A"


# ---------------------------------------------------------------------------
# fmt_ip
# ---------------------------------------------------------------------------

class TestFmtIp:
    def test_whole_innings(self):
        assert fmt_ip(33.0) == "33.0"
        assert fmt_ip(100.0) == "100.0"

    def test_partial_innings(self):
        assert fmt_ip(33.333) == "33.1"  # 1 out
        assert fmt_ip(33.667) == "33.2"  # 2 outs
        assert fmt_ip(6.667) == "6.2"

    def test_zero(self):
        assert fmt_ip(0.0) == "0.0"
        assert fmt_ip(0.333) == "0.1"

    def test_none(self):
        assert fmt_ip(None) == "-"

    def test_string_passthrough(self):
        assert fmt_ip("5.2") == "5.2"
        assert fmt_ip("") == "-"


# ---------------------------------------------------------------------------
# short_name
# ---------------------------------------------------------------------------

class TestShortName:
    def test_standard(self):
        assert short_name("Mike Trout") == "M. Trout"
        assert short_name("Fernando Tatis") == "F. Tatis"

    def test_suffix_jr(self):
        assert short_name("Fernando Tatis Jr.") == "F. Tatis Jr."
        assert short_name("Ken Griffey Jr") == "K. Griffey Jr"

    def test_suffix_sr(self):
        assert short_name("Cal Ripken Sr.") == "C. Ripken Sr."

    def test_suffix_numeral(self):
        assert short_name("Bobby Witt II") == "B. Witt II"
        assert short_name("Ronald Acuna III") == "R. Acuna III"

    def test_single_name(self):
        assert short_name("Madonna") == "Madonna"

    def test_multi_part(self):
        assert short_name("Jose de Leon") == "J. Leon"


# ---------------------------------------------------------------------------
# height_str
# ---------------------------------------------------------------------------

class TestHeightStr:
    def test_standard(self):
        assert height_str(188) == "6'2\""
        assert height_str(193) == "6'4\""
        assert height_str(175) == "5'9\""

    def test_none(self):
        assert height_str(None) is None
        assert height_str(0) is None


# ---------------------------------------------------------------------------
# fmt_pct
# ---------------------------------------------------------------------------

class TestFmtPct:
    def test_standard(self):
        assert fmt_pct(0.325) == "32.5%"
        assert fmt_pct(0.10) == "10.0%"

    def test_decimals(self):
        assert fmt_pct(0.3256, decimals=2) == "32.56%"

    def test_none(self):
        assert fmt_pct(None) == "—"


# ---------------------------------------------------------------------------
# fmt_avg
# ---------------------------------------------------------------------------

class TestFmtAvg:
    def test_standard(self):
        assert fmt_avg(0.325) == ".325"
        assert fmt_avg(0.300) == ".300"
        assert fmt_avg(0.2) == ".200"

    def test_none(self):
        assert fmt_avg(None) == "—"


# ---------------------------------------------------------------------------
# Position utilities
# ---------------------------------------------------------------------------

class TestPositionUtils:
    def test_display_pos_cof(self):
        assert display_pos("COF") == "OF"
        assert display_pos(PositionalBucket.COF) == "OF"

    def test_display_pos_others(self):
        assert display_pos("SS") == "SS"
        assert display_pos("SP") == "SP"
        assert display_pos(PositionalBucket.CF) == "CF"

    def test_next_level(self):
        assert next_level(2) == 1   # AAA → MLB
        assert next_level(3) == 2   # AA → AAA
        assert next_level(4) == 3   # A → AA
        assert next_level(1) is None  # MLB — no level above

    def test_level_key(self):
        assert level_key(1) == "mlb"
        assert level_key(2) == "aaa"
        assert level_key(4) == "a"
        assert level_key(8) == "intl"

    def test_level_display(self):
        assert level_display(1) == "MLB"
        assert level_display(2) == "AAA"
        assert level_display(8) == "International"

    def test_role_map(self):
        assert ROLE_MAP[11] == "SP"
        assert ROLE_MAP[12] == "RP"
        assert ROLE_MAP[13] == "CL"

    def test_game_pos_map(self):
        assert GAME_POS_MAP[1] == "P"
        assert GAME_POS_MAP[6] == "SS"
        assert GAME_POS_MAP[10] == "DH"

    def test_pitch_fields_count(self):
        assert len(PITCH_FIELDS) == 12
        assert len(PITCH_NAMES) == 12
        assert all(f in PITCH_NAMES for f in PITCH_FIELDS)

    def test_level_norm_age_coverage(self):
        # All level keys should have a norm age
        for key in LEVEL_KEY_MAP.values():
            if key in ("college", "hs"):
                continue  # These don't have norm ages (draft-only)
            assert key in LEVEL_NORM_AGE, f"Missing norm age for {key}"


# ---------------------------------------------------------------------------
# Rating normalization (pure functions)
# ---------------------------------------------------------------------------

from statsplusplus.config.ratings import (
    norm as config_norm,
    norm_continuous as config_norm_continuous,
    norm_floor as config_norm_floor,
)


class TestConfigRatings:
    """Tests for the new pure-function ratings module."""

    def test_norm_1_100_scale(self):
        """1-100 scale: 50 → maps to middle of 20-80."""
        result = config_norm(50, "1-100")
        assert result == 50  # 20 + (50/100)*60 = 50, rounds to 50

    def test_norm_20_80_scale(self):
        """20-80 scale: passes through with rounding."""
        assert config_norm(55, "20-80") == 55
        assert config_norm(53, "20-80") == 55  # rounds to nearest 5

    def test_norm_1_20_scale(self):
        """1-20 scale: 10 → maps to ~47 → rounds to 45 or 50."""
        result = config_norm(10, "1-20")
        assert result is not None
        assert 40 <= result <= 55

    def test_norm_none_input(self):
        assert config_norm(None) is None

    def test_norm_zero_input(self):
        assert config_norm(0) is None

    def test_norm_string_input(self):
        """String that can be int-parsed should work."""
        assert config_norm("65", "1-100") is not None

    def test_norm_invalid_string(self):
        assert config_norm("abc") is None

    def test_norm_continuous_no_rounding(self):
        """Continuous version preserves granularity."""
        result = config_norm_continuous(50, "1-100")
        assert result == pytest.approx(50.0)  # 20 + 50/100*60 = 50.0

    def test_norm_continuous_fractional(self):
        result = config_norm_continuous(75, "1-100")
        # 20 + 75/100*60 = 65.0
        assert result == pytest.approx(65.0)

    def test_norm_floor_returns_int(self):
        assert config_norm_floor(None, floor=30) == 30
        assert config_norm_floor(50, "1-100") == 50

    def test_matches_legacy(self):
        """Verify identical output to legacy scripts/ratings.py."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
        from ratings import norm as legacy_norm, norm_continuous as legacy_nc, init_ratings_scale

        for scale in ["1-100", "20-80", "1-20"]:
            init_ratings_scale(scale)
            for raw in [None, 0, 10, 20, 30, 50, 65, 80, 100]:
                new = config_norm(raw, scale)
                old = legacy_norm(raw)
                assert new == old, f"norm mismatch: raw={raw}, scale={scale}, new={new}, old={old}"

            for raw in [10, 30, 50, 75, 100]:
                new_c = config_norm_continuous(raw, scale)
                old_c = legacy_nc(raw)
                if new_c is not None and old_c is not None:
                    assert abs(new_c - old_c) < 0.01, f"continuous mismatch: raw={raw}, scale={scale}"
