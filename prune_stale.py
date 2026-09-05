#!/usr/bin/env python3
"""Prune stale code files left over from a previous version.

The zip distribution has no package manager: when a user extracts a new release
over an existing install, files that were *removed* in the new version linger on
disk and can shadow/conflict with the current package (e.g. an old
``scripts/db.py`` on sys.path shadowing ``statsplusplus.data.db``).

The release zip ships a ``MANIFEST.txt`` listing every file it contains. This
script deletes any ``.py`` file under the tracked code directories that is NOT
in the manifest — i.e. a leftover from a prior version. It is deliberately
conservative:

- Only runs when ``MANIFEST.txt`` exists (dev checkouts have none → no-op).
- Only ever touches ``.py`` files under PRUNE_DIRS.
- Never touches ``data/`` (user data), ``.venv/``, config, or anything else.

Invoked by start.sh / start.bat on launch. Safe to run repeatedly.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Code directories whose stale .py leftovers are safe to prune. Explicitly
# excludes data/, .venv/, config, docs, etc.
PRUNE_DIRS = ("scripts", "src", "web", "statsplus")


def prune(root: Path) -> list[str]:
    """Delete stale .py files under PRUNE_DIRS not present in MANIFEST.txt.

    Returns the list of removed relative paths. No-op (empty list) when the
    manifest is absent.
    """
    manifest_path = root / "MANIFEST.txt"
    if not manifest_path.exists():
        return []

    kept = {
        line.strip().replace("\\", "/")
        for line in manifest_path.read_text().splitlines()
        if line.strip()
    }

    removed: list[str] = []
    for d in PRUNE_DIRS:
        base = root / d
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if rel not in kept:
                try:
                    path.unlink()
                    removed.append(rel)
                except OSError:
                    pass  # best-effort; a locked/removed file isn't fatal
    return removed


def main() -> None:
    root = Path(__file__).resolve().parent
    removed = prune(root)
    if removed:
        print(f"  Cleaned up {len(removed)} stale file(s) from a previous version.")


if __name__ == "__main__":
    main()
