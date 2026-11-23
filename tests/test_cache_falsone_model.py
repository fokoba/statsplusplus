"""Reproducible model setup without credentials or writes to the live league."""
import io
import json
import tarfile
from pathlib import Path

import pytest
import cache_falsone_model as cache


def archive(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode='w:gz') as bundle:
        for name, contents in entries:
            info = tarfile.TarInfo(name)
            raw = contents.encode()
            info.size = len(raw)
            bundle.addfile(info, io.BytesIO(raw))
    return data.getvalue()


def test_source_extraction_keeps_only_required_code(tmp_path):
    entries = [('root/' + name, 'version = "1.25.0"' if name == 'pyproject.toml' else '# source')
               for name in cache.REQUIRED]
    entries += [('root/data/private.json', 'not source')]
    cache.extract_source(archive(entries), tmp_path)
    assert all((tmp_path / name).is_file() for name in cache.REQUIRED)
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('name', ['../escape.py', '/absolute.py', 'root/../escape.py'])
def test_unsafe_archive_paths_are_rejected(tmp_path, name):
    with pytest.raises(ValueError, match='Unsafe path'):
        cache.extract_source(archive([(name, 'bad')]), tmp_path)


def test_existing_pinned_cache_is_reused_without_network(tmp_path, monkeypatch):
    model = tmp_path / cache.PINNED_COMMIT
    for name in cache.REQUIRED:
        p = model / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('version = "1.25.0"' if name == 'pyproject.toml' else '# source')
    monkeypatch.setattr(cache, 'urlopen', lambda *a, **k: pytest.fail('Unexpected network request'))
    result = cache.cache_model(tmp_path)
    assert result['sha'] == cache.PINNED_COMMIT
    assert json.loads((tmp_path / 'current.json').read_text())['version'] == '1.25.0'


def test_invalid_commit_cannot_escape_cache(tmp_path):
    with pytest.raises(ValueError, match='complete lowercase'):
        cache.cache_model(tmp_path, '../other')
