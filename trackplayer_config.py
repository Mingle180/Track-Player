#!/usr/bin/env python3
"""Configuration for Track Player — built-in defaults + a user override file.

The user override lives at ``~/.trackplayer/config.json`` and is merged over the
built-in defaults. ``load_config()`` reads the file fresh on every call, so edits
take effect on the next refresh/play without restarting (hot reload).

Only the Python standard library is used. Speed presets are expressed in km/h
(the unit users think in) and converted to m/s for the playback core.
"""

import copy
import json
from pathlib import Path

DEFAULT_CONFIG = {
    # km/h presets offered in the GUI dropdown and `trackplayer play --preset`.
    "speed_presets": [
        {"label": "步行", "kmh": 5},
        {"label": "跑步", "kmh": 10},
        {"label": "骑行", "kmh": 20},
        {"label": "驾车", "kmh": 50},
    ],
    "default_speed_mps": 3.0,
    "default_scale": 2.0,
    "default_repeat": 1,
    # How often the last coordinate is re-asserted while holding position
    # (keep-alive against iOS fusion-location snap-back).
    "keepalive_interval_s": 1.0,
    # Minimum seconds between two state.json flushes while playing.
    "state_save_interval_s": 5.0,
}


def config_path():
    """Path of the user override file (may not exist)."""
    return Path.home() / '.trackplayer' / 'config.json'


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_config():
    """Defaults merged with the user override file, read fresh from disk.

    A missing or malformed override file falls back to the built-in defaults
    (a malformed file also returns an ``_error`` key describing the problem so
    the UI can surface it instead of failing silently).
    """
    path = config_path()
    try:
        with open(path, encoding='utf-8') as f:
            override = json.load(f)
    except FileNotFoundError:
        return copy.deepcopy(DEFAULT_CONFIG)
    except (OSError, json.JSONDecodeError) as e:
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['_error'] = f'config {path}: {e}'
        return cfg
    if not isinstance(override, dict):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        cfg['_error'] = f'config {path}: top-level JSON must be an object'
        return cfg
    return _merge(DEFAULT_CONFIG, override)


def speed_presets(config=None):
    """The list of ``{"label", "kmh"}`` speed presets from config."""
    cfg = config if config is not None else load_config()
    presets = cfg.get('speed_presets', DEFAULT_CONFIG['speed_presets'])
    return [p for p in presets if isinstance(p, dict) and 'kmh' in p]


def preset_speed_mps(preset):
    """Convert a km/h preset to the m/s value the playback core expects."""
    return float(preset['kmh']) / 3.6


def find_preset(name, config=None):
    """Look up a preset by its label (case-insensitive); None if not found."""
    for p in speed_presets(config):
        if str(p.get('label', '')).lower() == str(name).lower():
            return p
    return None