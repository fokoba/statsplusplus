"""Cache the tested public Falsone model without committing third-party source.

Usage: python3 scripts/cache_falsone_model.py
The pinned commit is intentional: refreshing raw ratings recomputes rankings,
while changing upstream code requires an explicit, reviewable version update.
"""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import tempfile
from urllib.request import Request, urlopen

PINNED_COMMIT = 'feba5fefb65b3505f5ff3d019aaa7d1af9aaa281'
REPOSITORY = 'https://github.com/tfalsone/statsplusplus'
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / 'data/upstream/falsone'
REQUIRED = ('LICENSE', 'pyproject.toml', 'scripts/draft_board.py',
            'src/statsplusplus/data/evaluation_engine.py',
            'src/statsplusplus/data/fv_calc.py')


def extract_source(archive: bytes, destination: Path) -> None:
    """Extract only regular upstream source files, rejecting unsafe paths."""
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:gz') as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('Unsafe path in upstream archive')
            parts = path.parts[1:]  # GitHub adds a single root directory.
            if not parts:
                continue
            if member.issym() or member.islnk():
                raise ValueError('Links are not allowed in the model cache')
            if not member.isfile():
                continue
            relative = Path(*parts)
            if parts[0] not in ('src', 'scripts') and str(relative) not in ('LICENSE', 'pyproject.toml'):
                continue
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError('Missing archive file data')
            with stream, target.open('wb') as output:
                shutil.copyfileobj(stream, output)
    missing = [name for name in REQUIRED if not (destination / name).is_file()]
    if missing:
        raise ValueError('Incomplete upstream model: ' + ', '.join(missing))


def cache_model(cache_dir: Path = DEFAULT_CACHE, commit: str = PINNED_COMMIT) -> dict:
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('Specify a complete lowercase Git commit SHA')
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / commit
    if not all((destination / name).is_file() for name in REQUIRED):
        if destination.exists():
            raise ValueError(f'Incomplete cache already exists: {destination}')
        request = Request(f'https://api.github.com/repos/tfalsone/statsplusplus/tarball/{commit}',
                          headers={'User-Agent': 'Dominator-Falsone-Model-Setup'})
        with urlopen(request, timeout=60) as response:
            archive = response.read()
        with tempfile.TemporaryDirectory(prefix='.install-', dir=cache_dir) as temporary:
            staged = Path(temporary) / 'source'
            staged.mkdir()
            extract_source(archive, staged)
            os.replace(staged, destination)
    project = (destination / 'pyproject.toml').read_text()
    version = re.search(r'(?m)^version\s*=\s*"([^"]+)"', project)
    if version is None:
        raise ValueError('Upstream project version is missing')
    metadata = {
        'sha': commit, 'version': version.group(1), 'repository': REPOSITORY,
        'calibration': 'PPL calibration files; upstream algorithms and default draft_value; no custom risk model or draft sliders',
    }
    # Preserve known commit-date metadata when refreshing the same installation.
    current = cache_dir / 'current.json'
    if current.exists():
        old = json.loads(current.read_text())
        if old.get('sha') == commit and old.get('date'):
            metadata['date'] = old['date']
    temporary = current.with_suffix(f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(metadata, indent=2) + '\n')
    os.replace(temporary, current)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit', default=PINNED_COMMIT)
    parser.add_argument('--cache-dir', type=Path, default=DEFAULT_CACHE)
    args = parser.parse_args()
    result = cache_model(args.cache_dir, args.commit)
    print(f"Falsone v{result['version']} cached at {args.cache_dir / result['sha']}")


if __name__ == '__main__':
    main()
