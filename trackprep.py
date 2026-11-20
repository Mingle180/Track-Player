#!/usr/bin/env python3
"""Prepare GPX/KML tracks for: pymobiledevice3 developer dvt simulate-location play"""

import argparse
import math
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
EARTH_R = 6371000.0
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


def gcj02_to_wgs84(lat, lon):
    if not (73.66 < lon < 135.05 and 3.86 < lat < 53.55):
        return lat, lon
    dlat = _tlat(lon - 105.0, lat - 35.0)
    dlon = _tlon(lon - 105.0, lat - 35.0)
    rad = lat / 180.0 * math.pi
    magic = 1 - GCJ_EE * math.sin(rad) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((GCJ_A * (1 - GCJ_EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (GCJ_A / sqrtmagic * math.cos(rad) * math.pi)
    return lat * 2 - (lat + dlat), lon * 2 - (lon + dlon)


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


def main():
    ap = argparse.ArgumentParser(
        description='Convert GPX/KML to a 1 Hz-timestamped GPX for pymobiledevice3 simulate-location play.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Play: pymobiledevice3 developer dvt simulate-location play OUT.gpx\n'
               'Restore: pymobiledevice3 developer dvt simulate-location clear\n'
               '\n'
               'Notes:\n'
               '  - Keep USB connected and the play command running; simulation stops on disconnect/Ctrl+C.\n'
               '  - Simulation replaces location device-wide; run clear afterwards (reboot always restores).\n'
               '  - Only GPS coordinates are simulated, not pedometer/motion sensors.\n'
               '  - Multiple devices: add --udid <UDID> to the pymobiledevice3 command.')
    ap.add_argument('input', help='input .gpx or .kml file')
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

    root = ET.parse(args.input).getroot()
    tag = local(root.tag).lower()
    if tag == 'gpx':
        points = parse_gpx(root)
    elif tag == 'kml':
        points = parse_kml(root)
    else:
        sys.exit(f'error: unsupported root element <{tag}>, expected gpx or kml')
    if len(points) < 2:
        sys.exit('error: need at least 2 track points')

    if args.from_gcj02:
        points = [(*gcj02_to_wgs84(lat, lon), t) for lat, lon, t in points]

    if args.speed is not None:
        points = resample(points, args.speed)
    elif args.scale is not None:
        if any(t is None for _, _, t in points):
            sys.exit('error: --scale needs timestamps on every point; use --speed instead')
        points = retime(points, args.scale)
    elif all(t is not None for _, _, t in points):
        points = normalize(points)
    else:
        print('note: missing timestamps, resampling at default --speed 3')
        points = resample(points, 3.0)

    points = repeat(points, args.repeat)

    out = args.output or str(Path(args.input).with_suffix('')) + '.play.gpx'
    write_gpx(out, points, Path(args.input).stem)
    dist = sum(haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
    dur = (points[-1][2] - points[0][2]).total_seconds()
    print(f'wrote {out}: {len(points)} points, {dist / 1000:.2f} km, {dur:.0f} s playback')


if __name__ == '__main__':
    main()