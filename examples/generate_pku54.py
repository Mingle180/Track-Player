#!/usr/bin/env python3
"""Generate the PKU May-4th sports-field 400 m loop (WGS-84 GPX, no timestamps).

Geometry verified against the real track by screenshot alignment:
standard 400 m stadium (lane-1 measure line R=36.8 m, half-straight S=42.195 m),
centered at the point below and rotated 4.1 deg CCW to match the track's true
orientation. Feed the output to trackprep.py to add 1 Hz timestamps, e.g.:

    python trackprep.py pku54_loop.gpx -o pku.play.gpx --speed 5 --repeat 3
"""

import argparse
import math
import xml.etree.ElementTree as ET

CENTER_LAT, CENTER_LON = 39.98631305, 116.3071460
R, S = 36.8, 42.195          # 2*(2*S) + 2*pi*R = 400.0 m
ROTATION_DEG = 4.1           # CCW, matches real track orientation
M_PER_DEG_LAT = 111320.0


def build_loop():
    pts = []

    def line(x1, y1, x2, y2):
        n = max(int(math.hypot(x2 - x1, y2 - y1) / 2.0), 1)
        for i in range(n):
            f = i / n
            pts.append((x1 + (x2 - x1) * f, y1 + (y2 - y1) * f))

    def arc(cx, cy, a1, a2):
        n = max(int(abs(a2 - a1) * R / 2.0), 1)
        for i in range(n):
            a = a1 + (a2 - a1) * i / n
            pts.append((cx + R * math.cos(a), cy + R * math.sin(a)))

    line(R, -S, R, S)                 # east straight, northbound
    arc(0, S, 0, math.pi)             # north semicircle
    line(-R, S, -R, -S)               # west straight, southbound
    arc(0, -S, math.pi, 2 * math.pi)  # south semicircle
    pts.append((R, -S))
    return pts


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('-o', '--output', default='pku54_loop.gpx')
    args = ap.parse_args()

    th = math.radians(ROTATION_DEG)
    c, s = math.cos(th), math.sin(th)
    mlon = M_PER_DEG_LAT * math.cos(math.radians(CENTER_LAT))

    gpx = ET.Element('gpx', {'version': '1.1', 'creator': 'generate_pku54',
                             'xmlns': 'http://www.topografix.com/GPX/1/1'})
    trk = ET.SubElement(gpx, 'trk')
    ET.SubElement(trk, 'name').text = 'PKU May-4th Field Loop'
    seg = ET.SubElement(trk, 'trkseg')
    for x, y in build_loop():
        xr, yr = x * c - y * s, x * s + y * c
        lat = CENTER_LAT + yr / M_PER_DEG_LAT
        lon = CENTER_LON + xr / mlon
        ET.SubElement(seg, 'trkpt', {'lat': f'{lat:.7f}', 'lon': f'{lon:.7f}'})
    ET.ElementTree(gpx).write(args.output, encoding='utf-8', xml_declaration=True)
    print(f'wrote {args.output}: {len(seg)} points, 400 m loop, rotation {ROTATION_DEG} deg CCW')


if __name__ == '__main__':
    main()