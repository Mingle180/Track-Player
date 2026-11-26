#!/usr/bin/env python3
"""Prepare GPX/KML/GeoJSON tracks for: pymobiledevice3 developer dvt simulate-location play"""

import argparse
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
EARTH_R = 6371000.0
M_PER_DEG_LAT = 111320.0
MAX_SCRIPT_POINTS = 1_000_000
GCJ_A = 6378245.0
GCJ_EE = 0.00669342162296594323


def local(tag):
    return tag.rsplit('}', 1)[-1]


def parse_time(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.strip().replace('Z', '+00:00'))
    except ValueError:
        return None


def parse_gpx(root):
    points = []
    for el in root.iter():
        if local(el.tag) not in ('trkpt', 'rtept'):
            continue
        lat, lon = el.get('lat'), el.get('lon')
        if lat is None or lon is None:
            continue
        t = None
        for child in el:
            if local(child.tag) == 'time':
                t = parse_time(child.text)
                break
        points.append((float(lat), float(lon), t))
    return points


def parse_kml(root):
    points = []
    for el in root.iter():
        tag = local(el.tag)
        if tag == 'LineString':
            for child in el:
                if local(child.tag) == 'coordinates':
                    for token in (child.text or '').split():
                        parts = token.split(',')
                        if len(parts) >= 2:
                            points.append((float(parts[1]), float(parts[0]), None))
        elif tag == 'Track':
            whens, coords = [], []
            for child in el:
                ct = local(child.tag)
                if ct == 'when':
                    whens.append(parse_time(child.text))
                elif ct == 'coord':
                    parts = (child.text or '').split()
                    if len(parts) >= 2:
                        coords.append((float(parts[1]), float(parts[0])))
            for i, (lat, lon) in enumerate(coords):
                points.append((lat, lon, whens[i] if i < len(whens) else None))
    return points


def parse_geojson(obj):
    """Collect (lat, lon, None) from GeoJSON LineString/MultiLineString/Point(s).

    GeoJSON has no standard per-point timestamps, so times are left None and the
    caller resamples by speed (same path as a timestamp-less GPX).
    """
    points = []

    def add_coord(c):
        if isinstance(c, (list, tuple)) and len(c) >= 2:
            points.append((float(c[1]), float(c[0]), None))

    def walk_geom(g):
        if not isinstance(g, dict):
            return
        t = g.get('type')
        coords = g.get('coordinates') or []
        if t == 'LineString':
            for c in coords:
                add_coord(c)
        elif t == 'MultiLineString':
            for line in coords:
                for c in line:
                    add_coord(c)
        elif t == 'Point':
            add_coord(coords)
        elif t == 'MultiPoint':
            for c in coords:
                add_coord(c)
        elif t == 'GeometryCollection':
            for sub in g.get('geometries') or []:
                walk_geom(sub)

    t = obj.get('type') if isinstance(obj, dict) else None
    if t == 'FeatureCollection':
        for f in obj.get('features') or []:
            walk_geom(f.get('geometry'))
    elif t == 'Feature':
        walk_geom(obj.get('geometry'))
    else:
        walk_geom(obj)
    return points


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(h))


def resample(points, speed):
    segs = []
    total = 0.0
    for (lat1, lon1, _), (lat2, lon2, _) in zip(points, points[1:]):
        d = haversine(lat1, lon1, lat2, lon2)
        if d > 0:
            segs.append((total, lat1, lon1, lat2, lon2, d))
            total += d
    if not segs:
        sys.exit('error: track has zero length')
    out = []
    idx = 0
    for i in range(int(total / speed) + 1):
        dist = i * speed
        while idx < len(segs) - 1 and segs[idx][0] + segs[idx][5] < dist:
            idx += 1
        cum, lat1, lon1, lat2, lon2, d = segs[idx]
        frac = min(max((dist - cum) / d, 0.0), 1.0)
        out.append((lat1 + (lat2 - lat1) * frac,
                    lon1 + (lon2 - lon1) * frac,
                    BASE_TIME + timedelta(seconds=i)))
    end_t = BASE_TIME + timedelta(seconds=total / speed)
    if out[-1][2] < end_t:
        out.append((points[-1][0], points[-1][1], end_t))
    return out


def normalize(points):
    t0 = points[0][2]
    return [(lat, lon, BASE_TIME + timedelta(seconds=(t - t0).total_seconds()))
            for lat, lon, t in points]


def retime(points, scale):
    return [(lat, lon, BASE_TIME + timedelta(seconds=(t - points[0][2]).total_seconds() / scale))
            for lat, lon, t in points]


def repeat(points, n):
    if n <= 1:
        return points
    duration = (points[-1][2] - points[0][2]).total_seconds()
    out = list(points)
    for r in range(1, n):
        offset = timedelta(seconds=(duration + 1.0) * r)
        out.extend((lat, lon, t + offset) for lat, lon, t in points)
    return out


def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return r


def _tlon(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return r


def _gcj_delta(lat, lon):
    dlat = _tlat(lon - 105.0, lat - 35.0)
    dlon = _tlon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - GCJ_EE * math.sin(rad) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (GCJ_A / sqrtmagic * math.cos(rad) * math.pi)
    return dlat, dlon


def gcj02_to_wgs84(lat, lon):
    if not (73.66 < lon < 135.05 and 3.86 < lat < 53.55):
        return lat, lon
    dlat, dlon = _gcj_delta(lat, lon)
    return lat - dlat, lon - dlon


def wgs84_to_gcj02(lat, lon):
    if not (73.66 < lon < 135.05 and 3.86 < lat < 53.55):
        return lat, lon
    dlat, dlon = _gcj_delta(lat, lon)
    return lat + dlat, lon + dlon


def write_gpx(path, points, name):
    gpx = ET.Element('gpx', {'version': '1.1', 'creator': 'trackprep',
                             'xmlns': 'http://www.topografix.com/GPX/1/1'})
    trk = ET.SubElement(gpx, 'trk')
    ET.SubElement(trk, 'name').text = name
    seg = ET.SubElement(trk, 'trkseg')
    for lat, lon, t in points:
        pt = ET.SubElement(seg, 'trkpt', {'lat': f'{lat:.7f}', 'lon': f'{lon:.7f}'})
        ET.SubElement(pt, 'time').text = t.isoformat().replace('+00:00', 'Z')
    ET.ElementTree(gpx).write(path, encoding='utf-8', xml_declaration=True)


def parse_track(input_path, from_gcj02=False):
    """Parse GPX/KML/GeoJSON into raw (lat, lon, time|None) WGS-84 vertices."""
    suffix = Path(input_path).suffix.lower()
    if suffix in ('.geojson', '.json'):
        try:
            with open(input_path, encoding='utf-8') as f:
                obj = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            sys.exit(f'error: invalid GeoJSON ({e})')
        points = parse_geojson(obj)
    else:
        root = ET.parse(input_path).getroot()
        tag = local(root.tag).lower()
        if tag == 'gpx':
            points = parse_gpx(root)
        elif tag == 'kml':
            points = parse_kml(root)
        else:
            sys.exit(f'error: unsupported root element <{tag}>, expected gpx or kml '
                     f'(use .geojson/.json suffix for GeoJSON)')
    if len(points) < 2:
        sys.exit('error: need at least 2 track points')
    if from_gcj02:
        points = [(*gcj02_to_wgs84(lat, lon), t) for lat, lon, t in points]
    return points


def cumulative_lengths(points):
    """Cumulative great-circle meters at each vertex (same length as points)."""
    cum = [0.0]
    for (lat1, lon1, _), (lat2, lon2, _) in zip(points, points[1:]):
        cum.append(cum[-1] + haversine(lat1, lon1, lat2, lon2))
    return cum


def point_at(points, cum, dist):
    """Interpolated (lat, lon) at cumulative distance dist, clamped to the track."""
    if dist <= 0:
        return points[0][:2]
    if dist >= cum[-1]:
        return points[-1][:2]
    end = bisect_left(cum, dist, lo=1)
    if dist == cum[end]:
        return points[end][:2]
    i = end - 1
    seg = cum[i + 1] - cum[i]
    frac = (dist - cum[i]) / seg if seg > 0 else 0.0
    lat1, lon1, _ = points[i]
    lat2, lon2, _ = points[i + 1]
    return lat1 + (lat2 - lat1) * frac, lon1 + (lon2 - lon1) * frac


def slice_track(points, cum, start_m, end_m):
    """Sub-polyline between two cumulative distances, endpoints interpolated."""
    lat1, lon1 = point_at(points, cum, start_m)
    lat2, lon2 = point_at(points, cum, end_m)
    out = [(lat1, lon1, None)]
    out.extend((lat, lon, None) for lat, lon, _ in
               points[bisect_right(cum, start_m):bisect_left(cum, end_m)])
    out.append((lat2, lon2, None))
    return out


def project_point(points, cum, lat, lon):
    """Snap (lat, lon) onto the polyline; return (along_m, snap_lat, snap_lon).

    Uses a local tangent plane anchored at the query point: exact enough for
    track-scale geometry and far cheaper than per-segment haversine iteration.
    """
    coslat = math.cos(math.radians(lat))
    best_d2 = float('inf')
    best = (0.0, points[0][0], points[0][1])
    for i in range(len(points) - 1):
        lat1, lon1, _ = points[i]
        lat2, lon2, _ = points[i + 1]
        ax = (lon1 - lon) * coslat * M_PER_DEG_LAT
        ay = (lat1 - lat) * M_PER_DEG_LAT
        bx = (lon2 - lon) * coslat * M_PER_DEG_LAT
        by = (lat2 - lat) * M_PER_DEG_LAT
        dx, dy = bx - ax, by - ay
        ll = dx * dx + dy * dy
        t = 0.0 if ll == 0 else min(1.0, max(0.0, -(ax * dx + ay * dy) / ll))
        px, py = ax + dx * t, ay + dy * t
        d2 = px * px + py * py
        if d2 < best_d2:
            best_d2 = d2
            best = (cum[i] + t * (cum[i + 1] - cum[i]),
                    lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)
    return best


def _script_number(value, n, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'error: leg {n} {field} must be a finite number')
    try:
        value = float(value)
    except OverflowError:
        value = math.inf
    if not math.isfinite(value):
        raise ValueError(f'error: leg {n} {field} must be a finite number')
    return value


def _script_plan(points, script, repeat_n):
    if not isinstance(script, list) or not script:
        raise ValueError('error: script must be a non-empty list of legs')
    cum = cumulative_lengths(points)
    total = cum[-1]
    if not math.isfinite(total) or total <= 0:
        raise ValueError('error: track has zero length')
    remaining = MAX_SCRIPT_POINTS // max(1, repeat_n) - 1
    cursor = 0.0
    steps = []
    for n, leg in enumerate(script, 1):
        if not isinstance(leg, dict):
            raise ValueError(f'error: leg {n} must be an object')
        kind = leg.get('type')
        if kind == 'move':
            to_m = _script_number(leg.get('to_m'), n, 'to_m')
            speed = _script_number(leg.get('speed'), n, 'speed')
            if speed <= 0:
                raise ValueError(f'error: leg {n} speed must be > 0')
            if not cursor < to_m <= total:
                raise ValueError(f'error: leg {n} to_m must be > {cursor:.1f} and <= {total:.1f}')
            seconds = (to_m - cursor) / speed
            if seconds > remaining:
                raise ValueError(f'error: leg {n} exceeds the {MAX_SCRIPT_POINTS:,}-point script limit')
            # Keep each full-speed step and reserve one tick for any remainder.
            whole = round(seconds)
            ticks = max(1, whole if abs(seconds - whole) < 1e-9 else math.ceil(seconds))
            steps.append((kind, ticks, cursor, to_m, speed))
            cursor = to_m
        elif kind == 'stay':
            ticks = leg.get('seconds')
            seed = leg.get('seed', 0)
            if type(ticks) is not int or ticks < 1:
                raise ValueError(f'error: leg {n} seconds must be an integer >= 1')
            if type(seed) is not int:
                raise ValueError(f'error: leg {n} seed must be an integer')
            jitter = _script_number(leg.get('jitter', 0.0), n, 'jitter')
            if jitter < 0:
                raise ValueError(f'error: leg {n} jitter must be >= 0')
            steps.append((kind, ticks, cursor, jitter, seed + n))
        else:
            raise ValueError(f'error: leg {n} type must be "move" or "stay"')
        remaining -= ticks
        if remaining < 0:
            raise ValueError(f'error: leg {n} exceeds the {MAX_SCRIPT_POINTS:,}-point script limit')
    return cum, steps


def validate_script(points, script, repeat_n=1):
    """Validate a script against the source geometry without allocating its points."""
    _script_plan(points, script, repeat_n)


def compile_script(points, script, repeat_n=1):
    """Compile a movement script into a strictly 1 Hz WGS-84 point list."""
    cum, steps = _script_plan(points, script, repeat_n)
    out = [(*points[0][:2], BASE_TIME)]
    for kind, ticks, cursor, target, value in steps:
        if kind == 'move':
            for tick in range(1, ticks + 1):
                dist = target if tick == ticks else cursor + tick * value
                out.append((*point_at(points, cum, dist), BASE_TIME + timedelta(seconds=len(out))))
        else:
            jitter, seed = target, value
            rng = random.Random(seed)
            alat, alon = point_at(points, cum, cursor)
            meters_per_lon = M_PER_DEG_LAT * math.cos(math.radians(alat))
            for _ in range(ticks):
                lat, lon = alat, alon
                if jitter:
                    lat += rng.gauss(0, jitter) / M_PER_DEG_LAT
                    lon += rng.gauss(0, jitter) / meters_per_lon
                    if not -90 <= lat <= 90 or not math.isfinite(lon):
                        raise ValueError('error: stay jitter produces invalid coordinates')
                    if not -180 <= lon <= 180:
                        lon = (lon + 180) % 360 - 180
                out.append((lat, lon, BASE_TIME + timedelta(seconds=len(out))))
    return out


def prepare(input_path, speed=None, scale=None, repeat_n=1, from_gcj02=False, script=None):
    """Parse a GPX/KML/GeoJSON file and return a 1 Hz-timestamped WGS-84 point list."""
    points = parse_track(input_path, from_gcj02)

    if script is not None:
        if speed is not None or scale is not None:
            raise ValueError('error: --script cannot be combined with --speed/--scale')
        points = compile_script(points, script, repeat_n)
    elif speed is not None:
        points = resample(points, speed)
    elif scale is not None:
        if any(t is None for _, _, t in points):
            sys.exit('error: --scale needs timestamps on every point; use --speed instead')
        points = retime(points, scale)
    elif all(t is not None for _, _, t in points):
        points = normalize(points)
    else:
        print('note: missing timestamps, resampling at default --speed 3')
        points = resample(points, 3.0)

    return repeat(points, repeat_n)


def main():
    ap = argparse.ArgumentParser(
        description='Convert GPX/KML/GeoJSON to a 1 Hz-timestamped GPX for pymobiledevice3 simulate-location play.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Play: pymobiledevice3 developer dvt simulate-location play OUT.gpx\n'
               'Restore: pymobiledevice3 developer dvt simulate-location clear\n'
               '\n'
               'Notes:\n'
               '  - Keep USB connected and the play command running; simulation stops on disconnect/Ctrl+C.\n'
               '  - Simulation replaces location device-wide; run clear afterwards (reboot always restores).\n'
               '  - Only GPS coordinates are simulated, not pedometer/motion sensors.\n'
               '  - Multiple devices: add --udid <UDID> to the pymobiledevice3 command.')
    ap.add_argument('input', help='input .gpx / .kml / .geojson / .json file')
    ap.add_argument('-o', '--output', help='output .gpx (default: <input>.play.gpx)')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--speed', type=float, metavar='MPS',
                   help='constant speed in m/s, resampled to 1 point/sec (e.g. 3 = running)')
    g.add_argument('--scale', type=float, metavar='X',
                   help='speed multiplier vs recorded timestamps (e.g. 2 = twice as fast)')
    ap.add_argument('--repeat', type=int, default=1, metavar='N',
                    help='repeat the track N times (default 1)')
    ap.add_argument('--from-gcj02', action='store_true',
                    help='input is GCJ-02 (AMap/Tencent); convert to WGS-84')
    args = ap.parse_args()

    if args.speed is not None and args.speed <= 0:
        ap.error('--speed must be > 0')
    if args.scale is not None and args.scale <= 0:
        ap.error('--scale must be > 0')
    if args.repeat < 1:
        ap.error('--repeat must be >= 1')

    points = prepare(args.input, args.speed, args.scale, args.repeat, args.from_gcj02)

    out = args.output or str(Path(args.input).with_suffix('')) + '.play.gpx'
    write_gpx(out, points, Path(args.input).stem)
    dist = sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
    dur = (points[-1][2] - points[0][2]).total_seconds()
    print(f'wrote {out}: {len(points)} points, {dist / 1000:.2f} km, {dur:.0f} s playback')


if __name__ == '__main__':
    main()
