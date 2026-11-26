#!/usr/bin/env python3
"""On-disk state for Track Player: last injected point + the track library.

Everything lives under ~/.trackplayer (next to config.json):
  state.json                    last simulated position (GCJ-02 display coords)
  tracks/<id>/source.<ext>      saved track, original bytes
  tracks/<id>/meta.json         name / created / suffix / from_gcj02 / script / km / n

Standard library only. Writes are atomic (tmp + os.replace); reads tolerate a
missing or corrupt file by returning an empty result. `root=` lets tests redirect
the whole store into a temp directory.

GPL-3.0-or-later, same as the rest of Track Player.
"""

import json
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SUFFIXES = ('.gpx', '.kml', '.geojson', '.json')
_UNCHANGED = object()


def store_root(root=None):
    return Path(root) if root is not None else Path.home() / '.trackplayer'


def state_path(root=None):
    return store_root(root) / 'state.json'


def tracks_dir(root=None):
    return store_root(root) / 'tracks'


def _read_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            obj = json.load(f)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         suffix='.tmp', delete=False) as f:
            tmp = Path(f.name)
            json.dump(obj, f, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(tmp, path)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _valid_point(p):
    if not isinstance(p, (list, tuple)) or len(p) != 2:
        return False
    lat, lon = p
    for v in (lat, lon):
        if type(v) not in (int, float):
            return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def load_last_point(root=None):
    obj = _read_json(state_path(root)) or {}
    p = obj.get('last_point')
    return [float(p[0]), float(p[1])] if _valid_point(p) else None


def save_last_point(lat, lon, root=None):
    _write_json(state_path(root), {
        'last_point': [float(lat), float(lon)],
        'saved_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })


def forget_last_point(root=None):
    state_path(root).unlink(missing_ok=True)


def _entry_dir(tid, root=None):
    if not isinstance(tid, str) or not tid.isascii() or not tid.isalnum() or len(tid) > 64:
        raise ValueError(f'invalid track id {tid!r}')
    return tracks_dir(root) / tid


def list_tracks(root=None):
    d = tracks_dir(root)
    if not d.is_dir():
        return []
    out = []
    for child in d.iterdir():
        try:
            meta, _ = load_track(child.name, root)
        except ValueError:
            continue
        out.append({
            'id': meta['id'],
            'name': meta.get('name', meta['id']),
            'created': meta.get('created', ''),
            'suffix': meta.get('suffix', '.gpx'),
            'from_gcj02': bool(meta.get('from_gcj02')),
            'has_script': bool(meta.get('script')),
            'km': meta.get('km', 0.0),
            'n': meta.get('n', 0),
        })
    out.sort(key=lambda t: t['created'], reverse=True)
    return out


def save_track(source_path, name, *, from_gcj02=False, script=None, km=0.0, n=0, root=None):
    suffix = Path(source_path).suffix.lower()
    if suffix not in SUFFIXES:
        raise ValueError('only .gpx / .kml / .geojson / .json files are supported')
    data = Path(source_path).read_bytes()
    if not data:
        raise ValueError('empty file')
    tid = uuid.uuid4().hex[:12]
    meta = {
        'id': tid,
        'name': str(name or 'track').strip() or 'track',
        'created': datetime.now(timezone.utc).isoformat(timespec='microseconds'),
        'suffix': suffix,
        'from_gcj02': bool(from_gcj02),
        'script': script or None,
        'km': float(km),
        'n': int(n),
    }
    parent = tracks_dir(root)
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.new-', dir=parent) as tmp:
        entry = Path(tmp) / 'entry'
        entry.mkdir()
        (entry / ('source' + suffix)).write_bytes(data)
        _write_json(entry / 'meta.json', meta)
        entry.rename(_entry_dir(tid, root))
    return meta


def load_track(tid, root=None):
    d = _entry_dir(tid, root)
    meta = _read_json(d / 'meta.json')
    if not meta or meta.get('id') != tid or meta.get('suffix') not in SUFFIXES:
        raise ValueError(f'track {tid} missing or invalid metadata')
    if not isinstance(meta.get('created'), str) or not isinstance(meta.get('name'), str):
        raise ValueError(f'track {tid} has invalid metadata')
    km, n = meta.get('km'), meta.get('n')
    if type(km) not in (int, float) or not 0 <= km < math.inf or type(n) is not int or n < 0:
        raise ValueError(f'track {tid} has invalid distance or point count')
    if type(meta.get('from_gcj02')) is not bool or not isinstance(meta.get('script'), (list, type(None))):
        raise ValueError(f'track {tid} has invalid script or coordinate system')
    src = d / ('source' + meta['suffix'])
    if not src.is_file():
        raise ValueError(f'track {tid} source file missing')
    return meta, src


def update_track(tid, *, name=None, from_gcj02=None, script=_UNCHANGED, km=None, root=None):
    meta, src = load_track(tid, root)
    if name is not None:
        meta['name'] = str(name).strip() or meta['name']
    if from_gcj02 is not None:
        meta['from_gcj02'] = bool(from_gcj02)
    if script is not _UNCHANGED:
        meta['script'] = script or None
    if km is not None:
        meta['km'] = km
    _write_json(src.parent / 'meta.json', meta)
    return meta


def delete_track(tid, root=None):
    d = _entry_dir(tid, root)
    if not d.is_dir():
        raise ValueError(f'track {tid} not found')
    shutil.rmtree(d)
