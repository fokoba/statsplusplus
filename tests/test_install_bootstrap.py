"""Regression: the web app must import even without an editable install.

Zip-install users run the launcher, which installs the package — but the app
also self-bootstraps `src/` onto sys.path so a missing/failed editable install
doesn't produce `ModuleNotFoundError: No module named 'statsplusplus'` (the
original fresh-install bug). This runs `web/app.py` in a subprocess with a
clean sys.path (only Flask available, package NOT installed) to prove the
bootstrap works on its own.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def test_web_app_imports_without_editable_install():
    """web/app.py must import with only Flask available (no statsplusplus on
    the installed path) — relying on its own src/ bootstrap."""
    # Run a subprocess whose environment does NOT expose the editable install:
    # PYTHONNODEBUGRANGES is irrelevant; the key is to strip the project's own
    # src/ (added by the `pip install -e .` .pth) from the inherited sys.path,
    # leaving Flask (in site-packages) intact. If the app still imports, its own
    # bootstrap did the work.
    src_dir = str(BASE / "src")
    snippet = (
        "import sys\n"
        f"sys.path = [p for p in sys.path if p != {src_dir!r}]\n"
        # Prove the editable install is neutralized: statsplusplus must NOT be
        # importable yet (only Flask, from site-packages, is).
        "import flask\n"
        "try:\n"
        "    import statsplusplus  # should fail — src/ was stripped\n"
        "    raise SystemExit('precondition failed: statsplusplus resolved without bootstrap')\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
        # Now import the app the way `python web/app.py` resolves it.
        f"sys.path.insert(0, {str(BASE)!r})\n"
        f"sys.path.insert(0, {str(BASE / 'web')!r})\n"
        "import app\n"
        "assert app.app is not None\n"
        "print('OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", snippet],
        capture_output=True, text=True, timeout=60, cwd=str(BASE),
    )
    assert proc.returncode == 0, (
        "web/app.py failed to import without an editable install:\n"
        f"{proc.stderr[-2000:]}"
    )
    assert "OK" in proc.stdout
