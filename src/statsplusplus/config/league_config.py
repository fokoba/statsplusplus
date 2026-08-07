"""League configuration class.

Provides typed access to all league-specific settings loaded from
league_settings.json and state.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from statsplusplus.config.league_context import get_league_dir
from statsplusplus.evaluation.constants import DEFAULT_MINIMUM_SALARY


class LeagueConfig:
    """Single source of truth for all league-specific settings."""

    def __init__(self, base_dir: Optional[Path] = None):
        self._base_dir = base_dir
        self._settings: Optional[dict[str, Any]] = None
        self._state: Optional[dict[str, Any]] = None
        self._mlb_tids: Optional[set[int]] = None

    def _resolve_paths(self) -> tuple[Path, Path]:
        d = self._base_dir or get_league_dir()
        return d / "config" / "league_settings.json", d / "config" / "state.json"

    def _load(self) -> None:
        if self._settings is None:
            settings_path, state_path = self._resolve_paths()
            self._settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
            self._state = json.loads(state_path.read_text()) if state_path.exists() else {}

    @property
    def _s(self) -> dict[str, Any]:
        self._load()
        return self._settings  # type: ignore[return-value]

    @property
    def _st(self) -> dict[str, Any]:
        self._load()
        return self._state  # type: ignore[return-value]

    def reload(self) -> None:
        self._settings = None
        self._state = None
        self._mlb_tids = None

    # --- State ---
    @property
    def my_team_id(self) -> int:
        return self._st.get("my_team_id", self._s.get("default_team_id", 0))

    @property
    def year(self) -> int:
        return self._st.get("year", self._s.get("year", 2033))

    @property
    def game_date(self) -> Optional[str]:
        return self._st.get("game_date")

    # --- Mappings ---
    @property
    def pos_map(self) -> dict[int, str]:
        return {int(k): v for k, v in self._s.get("pos_map", {}).items()}

    @property
    def role_map(self) -> dict[int, str]:
        return {int(k): v for k, v in self._s.get("role_map", {}).items()}

    @property
    def level_map(self) -> dict[str, str]:
        return dict(self._s.get("level_map", {}))

    @property
    def pos_order(self) -> dict[str, int]:
        return self._s.get("pos_order", {
            "SP": 1, "RP": 2, "CL": 3, "P": 1, "C": 4, "1B": 5,
            "2B": 6, "3B": 7, "SS": 8, "LF": 9, "CF": 10, "RF": 11, "OF": 10, "DH": 12,
        })

    @property
    def pyth_exp(self) -> float:
        return self._s.get("pyth_exp", 1.83)

    @property
    def minimum_salary(self) -> int:
        return self._s.get("minimum_salary", DEFAULT_MINIMUM_SALARY)

    @property
    def ratings_scale(self) -> str:
        return self._s.get("ratings_scale", "1-100")

    # --- Teams ---
    @property
    def divisions(self) -> dict[str, list[int]]:
        return self._s.get("divisions", {})

    @property
    def leagues(self) -> list[dict[str, Any]]:
        if "leagues" in self._s:
            return self._s["leagues"]
        divs = self._s.get("divisions", {})
        by_league: dict[str, dict[str, Any]] = {}
        for full_name, tids in divs.items():
            parts = full_name.split(" ", 1)
            lg_short, div_name = (parts[0], parts[1]) if len(parts) == 2 else ("League", full_name)
            if lg_short not in by_league:
                by_league[lg_short] = {"name": lg_short, "short": lg_short, "color": "#508cff", "divisions": {}}
            by_league[lg_short]["divisions"][div_name] = tids
        return list(by_league.values())

    def league_for_team(self, tid: int) -> Optional[dict[str, Any]]:
        for lg in self.leagues:
            for tids in lg["divisions"].values():
                if tid in tids:
                    return lg
        return None

    @property
    def team_abbr_map(self) -> dict[int, str]:
        abbr = {int(k): v for k, v in self._s.get("team_abbr", {}).items()}
        if not abbr:
            from statsplusplus.data.db import get_connection
            conn = get_connection(self._base_dir)
            rows = conn.execute("SELECT team_id, name FROM teams").fetchall()
            abbr = {r[0]: r[1][:3].upper() for r in rows}
        return abbr

    @property
    def team_names_map(self) -> dict[int, str]:
        names = {int(k): v for k, v in self._s.get("team_names", {}).items()}
        if not names:
            from statsplusplus.data.db import get_connection
            conn = get_connection(self._base_dir)
            rows = conn.execute("SELECT team_id, name FROM teams").fetchall()
            names = {r[0]: r[1] for r in rows}
        return names

    @property
    def team_div_map(self) -> dict[int, str]:
        return {tid: div for div, tids in self.divisions.items() for tid in tids}

    @property
    def mlb_team_ids(self) -> set[int]:
        if self._mlb_tids is None:
            configured = set(self.team_names_map.keys())
            from statsplusplus.data.db import get_connection
            conn = get_connection(self._base_dir)
            rows = conn.execute("SELECT DISTINCT team_id FROM players WHERE level='1'").fetchall()
            db_tids = {r[0] for r in rows}
            self._mlb_tids = db_tids & configured if configured else db_tids
        return self._mlb_tids

    def team_name(self, tid: int) -> str:
        return self.team_names_map.get(tid, "?")

    def team_abbr(self, tid: int) -> str:
        return self.team_abbr_map.get(tid, "?")

    def division(self, tid: int) -> str:
        return self.team_div_map.get(tid, "")

    # --- Raw access ---
    @property
    def settings(self) -> dict[str, Any]:
        return self._s

    @property
    def perpetual_arb(self) -> bool:
        return self._s.get("perpetual_arb", False)

    @property
    def state_path(self) -> Path:
        _, sp = self._resolve_paths()
        return sp

    @property
    def league_dir(self) -> Path:
        return self._base_dir or get_league_dir()

    @property
    def has_extended_ratings(self) -> bool:
        if not hasattr(self, "_has_extended"):
            from statsplusplus.data.db import get_connection
            conn = get_connection(self.league_dir)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(ratings)").fetchall()}
            self._has_extended = "babip" in cols and conn.execute(
                "SELECT 1 FROM ratings WHERE babip IS NOT NULL LIMIT 1"
            ).fetchone() is not None
        return self._has_extended
