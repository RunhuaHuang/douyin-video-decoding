"""Resolve explicitly catalogued old media without falling back to a download.

The catalogue is a portable JSON snapshot, never a SQLite database on SMB.
Same-byte aliases and user-selected work primaries are deliberately separate.
"""
from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path, PurePosixPath

CATALOG_NAME = '.douyin-media-catalog.json'


class CatalogLookupError(ValueError):
    pass


def _signature(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


@lru_cache(maxsize=16)
def _read(path, signature):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if data.get('version') != 1 or not isinstance(data.get('aliases'), dict):
            raise ValueError('unsupported catalogue')
        if not isinstance(data.get('primaries'), dict) or not isinstance(data.get('roots'), dict):
            raise ValueError('invalid catalogue mappings')
        return data
    except (OSError, ValueError) as exc:
        raise CatalogLookupError('媒体目录索引不可读，暂缓复用，不重新下载') from exc


@lru_cache(maxsize=4096)
def _verified(path, signature, expected):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    if _signature(Path(path)) != signature or h.hexdigest() != expected:
        raise CatalogLookupError('媒体目录目标已变化或哈希不符，暂缓复用，不重新下载')
    return True


def locate_media(out_dir: Path, work_id: str):
    """Return a validated catalogue entry, None if uncatalogued; fail closed otherwise."""
    directory = Path(out_dir).resolve()
    for base in (directory, *directory.parents):
        catalogue = base / CATALOG_NAME
        if not catalogue.exists():
            continue
        data = _read(str(catalogue), _signature(catalogue))
        local_root = data.get('local_root')
        entry = data['primaries'].get(str(work_id))
        if entry is None:
            relative = directory.relative_to(base)
            for label in ('视频', '图文'):
                key = local_root + '\0' + (relative / f'{label}-{work_id}.mp4').as_posix()
                if key in data['aliases']:
                    entry = data['aliases'][key]
                    break
        if entry is None:
            return None
        rel = PurePosixPath(entry['relative_path'])
        if rel.is_absolute() or '..' in rel.parts or str(entry.get('work_id')) != str(work_id):
            raise CatalogLookupError('媒体目录目标身份不符，暂缓复用，不重新下载')
        root = entry['storage_root']
        bases = [base] if root == local_root else []
        bases += [Path(p) for p in data['roots'].get(root, [])]
        for root_path in dict.fromkeys(bases):
            if not root_path.is_dir():
                continue
            target = root_path / Path(rel)
            if not target.exists():
                continue
            if not target.resolve().is_relative_to(root_path.resolve()):
                raise CatalogLookupError('媒体目录目标越界，暂缓复用，不重新下载')
            try:
                _verified(str(target), _signature(target), entry['sha256'])
            except OSError as exc:
                raise CatalogLookupError('媒体目录目标读取失败，暂缓复用，不重新下载') from exc
            return {**entry, 'path': target}
        raise CatalogLookupError('媒体目录目标或挂载不可用，暂缓复用，不重新下载')
    return None
