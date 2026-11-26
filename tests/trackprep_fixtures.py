"""Shared fixtures: temporary GPX/GeoJSON files and a straight north-bound line."""

import json
import math
import os
import tempfile
from datetime import timedelta
from pathlib import Path

import trackprep

# Match haversine's own metres-per-degree so north_line(m) measures m, not 0.999*m.
M_PER_DEG = trackprep.EARTH_R * math.pi / 180.0


def tmp_gpx(points, times=False):
    """points: [(lat, lon)] → path of a temp .gpx (optionally 1 Hz timestamped)."""
    fd, path = tempfile.mkstemp(suffix='.gpx', prefix='tp_test_')
    os.close(fd)
    rows = []
    for i, (lat, lon) in enumerate(points):
        t = ''
        if times:
            stamp = (trackprep.BASE_TIME + timedelta(seconds=i)).isoformat().replace('+00:00', 'Z')
            t = f'<time>{stamp}</time>'
        rows.append(f'<trkpt lat="{lat:.7f}" lon="{lon:.7f}">{t}</trkpt>')
    Path(path).write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="test" xmlns="http://www.topografix.com/GPX/1/1">'
        '<trk><name>t</name><trkseg>' + ''.join(rows) + '</trkseg></trk></gpx>',
        encoding='utf-8')
    return path


def tmp_geojson(coords):
    """coords: [[lon, lat], ...] → path of a temp .geojson LineString."""
    fd, path = tempfile.mkstemp(suffix='.geojson', prefix='tp_test_')
    os.close(fd)
    Path(path).write_text(json.dumps(
        {'type': 'Feature', 'geometry': {'type': 'LineString', 'coordinates': coords}}),
        encoding='utf-8')
    return path


def north_line(meters, steps=10, lat=39.99, lon=116.30):
    """steps+1 vertices heading due north, ~meters long."""
    return [(lat + meters * i / steps / M_PER_DEG, lon) for i in range(steps + 1)]