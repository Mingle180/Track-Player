#!/usr/bin/env python3
"""Generate a running track around the White House fence (GPX + KML, 1 Hz)."""

import argparse
import math
import random
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

CENTER_LAT, CENTER_LON = 38.89755, -77.03655
HALF_W, HALF_H, CORNER_R = 195.0, 139.0, 12.0  # meters, sidewalk loop
M_PER_DEG_LAT = 111320.0
START_TIME = datetime(2026, 11, 16, 12, 30, tzinfo=timezone.utc)  # 07:30 EST


def build_loop():
    """Dense clockwise loop in local meters (x=east, y=north), start east side near NE."""
    pts = []

    def line(x1, y1, x2, y2):
        d = math.hypot(x2 - x1, y2 - y1)
        n = max(int(d / 0.25), 1)
        for i in range(n):
            f = i / n
            pts.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, False))

    def arc(cx, cy, a1, a2):
        n = max(int(abs(a2 - a1) * CORNER_R / 0.25), 1)
        for i in range(n):
            a = a1 + (a2 - a1) * i / n
            pts.append((cx + CORNER_R * math.cos(a), cy + CORNER_R * math.sin(a), True))

    W, H, r = HALF_W, HALF_H, CORNER_R
    line(W, H - r, W, -(H - r))
    arc(W - r, -(H - r), 0, -math.pi / 2)
    line(W - r, -H, -(W - r), -H)
    arc(-(W - r), -(H - r), -math.pi / 2, -math.pi)
    line(-W, -(H - r), -W, H - r)
    arc(-(W - r), H - r, math.pi, math.pi / 2)
    line(-(W - r), H, W - r, H)
    arc(W - r, H - r, math.pi / 2, 0)
    pts.append((W, H - r, False))
    cum = [0.0]
    for (x1, y1, _), (x2, y2, _) in zip(pts, pts[1:]):
        cum.append(cum[-1] + math.hypot(x2 - x1, y2 - y1))
    return pts, cum


def position_at(pts, cum, dist):
    dist %= cum[-1]
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= dist:
            lo = mid
        else:
            hi = mid
    f = (dist - cum[lo]) / (cum[lo + 1] - cum[lo])
    x1, y1, c1 = pts[lo]
    x2, y2, _ = pts[lo + 1]
    return x1 + (x2 - x1) * f, y1 + (y2 - y1) * f, c1


def generate(laps, speed, seed):
    rng = random.Random(seed)
    pts, cum = build_loop()
    lap_len = cum[-1]
    total = laps * lap_len
    out = []
    dist = 0.0
    t = 0
    while dist < total:
        x, y, corner = position_at(pts, cum, dist)
        v = speed * (0.88 if corner else 1.0)
        v *= 1 + 0.07 * math.sin(2 * math.pi * t / 89) + 0.03 * math.sin(2 * math.pi * t / 23 + 1.7)
        jx, jy = rng.gauss(0, 0.6), rng.gauss(0, 0.6)
        lat = CENTER_LAT + (y + jy) / M_PER_DEG_LAT
        lon = CENTER_LON + (x + jx) / (M_PER_DEG_LAT * math.cos(math.radians(CENTER_LAT)))
        ele = 21.0 + 1.2 * math.sin(2 * math.pi * dist / 400) + rng.gauss(0, 0.3)
        out.append((lat, lon, ele, START_TIME + timedelta(seconds=t)))
        dist += v
        t += 1
    return out, lap_len


def write_gpx(path, points, name):
    gpx = ET.Element('gpx', {'version': '1.1', 'creator': 'whitehouse-run',
                             'xmlns': 'http://www.topografix.com/GPX/1/1'})
    meta = ET.SubElement(gpx, 'metadata')
    ET.SubElement(meta, 'name').text = name
    ET.SubElement(meta, 'time').text = points[0][3].isoformat().replace('+00:00', 'Z')
    trk = ET.SubElement(gpx, 'trk')
    ET.SubElement(trk, 'name').text = name
    ET.SubElement(trk, 'type').text = 'running'
    seg = ET.SubElement(trk, 'trkseg')
    for lat, lon, ele, t in points:
        pt = ET.SubElement(seg, 'trkpt', {'lat': f'{lat:.7f}', 'lon': f'{lon:.7f}'})
        ET.SubElement(pt, 'ele').text = f'{ele:.1f}'
        ET.SubElement(pt, 'time').text = t.isoformat().replace('+00:00', 'Z')
    ET.ElementTree(gpx).write(path, encoding='utf-8', xml_declaration=True)


def write_kml(path, points, name):
    kml = ET.Element('kml', {'xmlns': 'http://www.opengis.net/kml/2.2',
                             'xmlns:gx': 'http://www.google.com/kml/ext/2.2'})
    doc = ET.SubElement(kml, 'Document')
    ET.SubElement(doc, 'name').text = name
    pm = ET.SubElement(doc, 'Placemark')
    ET.SubElement(pm, 'name').text = name
    track = ET.SubElement(pm, 'gx:Track')
    for lat, lon, ele, t in points:
        ET.SubElement(track, 'when').text = t.isoformat().replace('+00:00', 'Z')
        ET.SubElement(track, 'gx:coord').text = f'{lon:.7f} {lat:.7f} {ele:.1f}'
    ET.ElementTree(kml).write(path, encoding='utf-8', xml_declaration=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--laps', type=int, default=3)
    ap.add_argument('--speed', type=float, default=3.0, help='base speed m/s (3.0 = 5:33/km)')
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    points, lap_len = generate(args.laps, args.speed, args.seed)
    name = f'White House Loop x{args.laps}'
    write_gpx('whitehouse_run.gpx', points, name)
    write_kml('whitehouse_run.kml', points, name)
    dur = (points[-1][3] - points[0][3]).total_seconds()
    dist = args.laps * lap_len
    pace = dur / 60 / (dist / 1000)
    print(f'lap {lap_len:.0f} m | {len(points)} points | {dist / 1000:.2f} km | '
          f'{dur / 60:.1f} min | avg pace {int(pace)}:{int(pace % 1 * 60):02d}/km')


if __name__ == '__main__':
    main()