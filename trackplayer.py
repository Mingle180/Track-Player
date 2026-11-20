#!/usr/bin/env python3
"""Track Player — one-command iPhone GPS track simulation on Windows.

Wraps trackprep (preprocessing) + pymobiledevice3 (DVT LocationSimulation):
prepares a GPX/KML track, auto-detects the device, plays it, and restores real
location automatically on exit (ENTER / Ctrl+C / --duration / error).

pymobiledevice3 is invoked as an external program (python -m pymobiledevice3),
not imported, so this file stays MIT-licensed.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import trackprep

PMD = [sys.executable, '-m', 'pymobiledevice3']


def run_pmd(args, **kw):
    return subprocess.run(PMD + args, **kw)


def list_devices():
    r = run_pmd(['usbmux', 'list'], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit('error: cannot list devices (is iTunes / Apple Mobile Device Support running?):\n'
                 + (r.stderr or r.stdout).strip())
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        sys.exit('error: unexpected `usbmux list` output')


def pick_device(udid):
    devs = list_devices()
    if not devs:
        sys.exit('error: no iPhone detected. Connect via USB, unlock, and trust this computer.')
    ids = [d.get('Identifier', '?') for d in devs]
    if udid:
        if udid not in ids:
            sys.exit(f'error: device {udid} not found. Connected: ' + ', '.join(ids))
        return udid
    if len(devs) > 1:
        sys.exit('error: multiple devices detected; pick one with --udid:\n'
                 + '\n'.join('  ' + d.get('Identifier', '?') + '  ' + d.get('DeviceName', '') for d in devs))
    return ids[0]


def restore(udid):
    args = ['developer', 'dvt', 'simulate-location', 'clear']
    if udid:
        args += ['--udid', udid]
    r = run_pmd(args, capture_output=True, text=True)
    if r.returncode != 0:
        print('warning: clear failed; reboot the phone to force-restore real location.\n'
              + (r.stderr or r.stdout).strip(), file=sys.stderr)


def cmd_play(args):
    if args.speed is not None and args.speed <= 0:
        sys.exit('error: --speed must be > 0')
    if args.scale is not None and args.scale <= 0:
        sys.exit('error: --scale must be > 0')
    if args.repeat < 1:
        sys.exit('error: --repeat must be >= 1')
    if args.duration is not None and args.duration <= 0:
        sys.exit('error: --duration must be > 0')

    udid = pick_device(args.udid)
    points = trackprep.prepare(args.input, args.speed, args.scale, args.repeat, args.from_gcj02)

    if args.output:
        out, keep = args.output, True
    else:
        fd, out = tempfile.mkstemp(suffix='.gpx', prefix='trackplayer_')
        os.close(fd)
        keep = False
    trackprep.write_gpx(out, points, Path(args.input).stem)
    dist = sum(trackprep.haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
    dur = (points[-1][2] - points[0][2]).total_seconds()
    print(f'device {udid} | {len(points)} points | {dist / 1000:.2f} km | {dur:.0f} s track')
    interactive = not args.duration and sys.stdin.isatty()
    if args.duration:
        print(f'playing for {args.duration:g}s, then auto-restoring real location...')
    elif interactive:
        print('playing... press ENTER to stop and restore real location.')
    else:
        print('playing... press Ctrl+C to stop and restore real location.')

    play_args = ['developer', 'dvt', 'simulate-location', 'play', out]
    if udid:
        play_args += ['--udid', udid]
    # Hold the child's stdin open via a pipe so it never sees EOF (which would
    # drop the simulation); the parent owns the console for ENTER detection.
    proc = subprocess.Popen(PMD + play_args, stdin=subprocess.PIPE)
    stop = threading.Event()
    if interactive:
        def watch_enter():
            try:
                input()
            except (EOFError, OSError):
                pass
            stop.set()
        threading.Thread(target=watch_enter, daemon=True).start()
    try:
        if args.duration:
            end = time.monotonic() + args.duration
            while proc.poll() is None and time.monotonic() < end:
                time.sleep(0.2)
        elif interactive:
            while proc.poll() is None and not stop.is_set():
                stop.wait(0.2)
        else:
            proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        print('restoring real location...')
        restore(udid)
        if not keep:
            try:
                os.remove(out)
            except OSError:
                pass
    print('done. real location restored.')


def cmd_clear(args):
    udid = pick_device(args.udid)
    restore(udid)
    print('real location restored.')


def cmd_devices(args):
    devs = list_devices()
    if not devs:
        print('no iPhone detected.')
        return
    for d in devs:
        print(f"{d.get('Identifier', '?')}  {d.get('DeviceName', '')}  "
              f"{d.get('ProductType', '')}  iOS {d.get('ProductVersion', '')}  {d.get('ConnectionType', '')}")


def main():
    ap = argparse.ArgumentParser(
        prog='trackplayer',
        description='Simulate iPhone GPS along a GPX/KML track on Windows; auto-restores real location on exit.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  trackplayer play route.kml --speed 3 --repeat 2\n'
               '  trackplayer play amap.gpx --from-gcj02 --speed 3\n'
               '  trackplayer play loop.gpx --speed 5 --duration 60\n'
               '  trackplayer devices\n'
               '  trackplayer clear\n'
               '\n'
               'Requires iTunes (usbmux service) + an iPhone with Developer Mode enabled, USB-connected.\n'
               'Only GPS coordinates are simulated, not pedometer/motion sensors.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('play', help='prepare a track and play it on the device')
    p.add_argument('input', help='input .gpx or .kml file')
    p.add_argument('-o', '--output', help='keep the prepared .gpx at this path (default: temp file, auto-removed)')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--speed', type=float, metavar='MPS', help='constant speed in m/s, resampled to 1 Hz (e.g. 3 = running)')
    g.add_argument('--scale', type=float, metavar='X', help='speed multiplier vs recorded timestamps (e.g. 2 = twice as fast)')
    p.add_argument('--repeat', type=int, default=1, metavar='N', help='repeat the track N times (default 1)')
    p.add_argument('--from-gcj02', action='store_true', help='input is GCJ-02 (AMap/Tencent); convert to WGS-84')
    p.add_argument('--udid', help='target device UDID (required only when multiple devices are connected)')
    p.add_argument('--duration', type=float, metavar='SEC', help='auto-stop and restore after SEC seconds (default: play until ENTER)')
    p.set_defaults(func=cmd_play)

    c = sub.add_parser('clear', help='restore real location now')
    c.add_argument('--udid', help='target device UDID')
    c.set_defaults(func=cmd_clear)

    d = sub.add_parser('devices', help='list connected iPhones')
    d.set_defaults(func=cmd_devices)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()