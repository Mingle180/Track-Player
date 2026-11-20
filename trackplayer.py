#!/usr/bin/env python3
"""Track Player — one-command iPhone GPS track simulation on Windows.

Wraps trackprep (preprocessing) + pymobiledevice3 (DVT LocationSimulation):
prepares a GPX/KML track, auto-detects the device, plays it over a persistent
DVT location channel that is held open for the whole session, and restores real
location automatically on exit (ENTER / Ctrl+C / --duration / error). While
playing, the current coordinate is re-asserted on a keep-alive interval to resist
iOS fusion-location snap-back; after the track ends the last position is held
until stopped.

The UI-agnostic core is play_track(); the CLI (cmd_play/clear/devices) and the
Tkinter GUI (trackplayer_gui) are thin wrappers around it.

This module imports pymobiledevice3 (GPL-3.0) directly to hold the location
channel open, so Track Player as a whole is distributed under GPL-3.0. Device
listing still shells out to the pymobiledevice3 CLI — a fast, tunnel-free query.
"""

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import threading
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import trackprep
import trackplayer_config as config

PMD = [sys.executable, '-m', 'pymobiledevice3']


def run_pmd(args, **kw):
    return subprocess.run(PMD + args, **kw)


def list_devices():
    r = run_pmd(['usbmux', 'list'], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError('error: cannot list devices (is iTunes / Apple Mobile Device Support running?):\n'
                           + (r.stderr or r.stdout).strip())
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise RuntimeError('error: unexpected `usbmux list` output') from None


def pick_device(udid):
    devs = list_devices()
    if not devs:
        raise RuntimeError('error: no iPhone detected. Connect via USB, unlock, and trust this computer.')
    ids = [d.get('Identifier', '?') for d in devs]
    if udid:
        if udid not in ids:
            raise RuntimeError(f'error: device {udid} not found. Connected: ' + ', '.join(ids))
        return udid
    if len(devs) > 1:
        raise RuntimeError('error: multiple devices detected; pick one with --udid:\n'
                           + '\n'.join('  ' + d.get('Identifier', '?') + '  ' + d.get('DeviceName', '') for d in devs))
    return ids[0]


# ---- persistent DVT location channel (imports pymobiledevice3; GPL-3.0) ----

class _LogForwarder(logging.Handler):
    """Forward pymobiledevice3 log records to the core's on_log callback."""

    def __init__(self, on_log):
        super().__init__(level=logging.INFO)
        self._on_log = on_log
        self.setFormatter(logging.Formatter('%(message)s'))

    def emit(self, record):
        try:
            self._on_log(record.getMessage())
        except Exception:
            pass


def _forward_pmd_logs(on_log):
    logger = logging.getLogger('pymobiledevice3')
    handler = _LogForwarder(on_log)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    # DTX frame traffic is extremely chatty; keep it out of the UI log.
    logging.getLogger('pymobiledevice3.dtx').setLevel(logging.WARNING)
    return logger, handler


@asynccontextmanager
async def _location_channel(serial):
    """Open a no-root userspace tunnel + DVT channel and yield LocationSimulation.

    The tunnel and channel stay open for the whole ``async with`` block (the
    persistent channel); closing them drops the simulation. One userspace tunnel
    per process is supported, so callers run this sequentially.
    """
    from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    async with UserspaceRsdTunnel(serial=serial) as rsd:
        async with DvtProvider(rsd) as dvt, LocationSimulation(dvt) as ls:
            yield ls


async def _restore_async(udid):
    async with _location_channel(udid) as ls:
        await ls.clear()


def restore(udid):
    try:
        asyncio.run(_restore_async(udid))
    except Exception as e:
        print('warning: clear failed; reboot the phone to force-restore real location.\n'
              + str(e).strip(), file=sys.stderr)


async def _play_async(points, udid, duration, stop_event, on_log, keepalive):
    loop = asyncio.get_running_loop()
    end = loop.time() + duration if duration else None

    def stopped():
        if stop_event is not None and stop_event.is_set():
            return True
        return end is not None and loop.time() >= end

    async def nap(seconds):
        # Sleep in small slices so a stop/duration deadline is honoured promptly.
        deadline = loop.time() + seconds
        while not stopped():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(0.1, remaining))

    async with _location_channel(udid) as ls:
        try:
            last = None
            prev_t = None
            for lat, lon, t in points:
                if stopped():
                    break
                if prev_t is not None and t is not None:
                    dt = (t - prev_t).total_seconds()
                    if dt > 0:
                        await nap(dt)
                        if stopped():
                            break
                await ls.set(lat, lon)
                on_log(f'set location to {lat:.6f} {lon:.6f}')
                last = (lat, lon)
                prev_t = t
            # Track finished: hold the last position, re-asserting it on the
            # keep-alive interval until stopped (also resists snap-back at rest).
            if last is not None:
                on_log(f'track finished; holding {last[0]:.6f} {last[1]:.6f} '
                       f'(keep-alive every {keepalive:g}s)...')
                while not stopped():
                    await ls.set(last[0], last[1])
                    await nap(keepalive)
        finally:
            on_log('restoring real location...')
            with suppress(Exception):
                await ls.clear()


def play_track(input_path, *, speed=None, scale=None, repeat=1, from_gcj02=False,
               udid=None, duration=None, output=None,
               stop_event=None, on_log=None, notice=None):
    """Prepare a track, play it on the device, and restore real location on exit.

    UI-agnostic core shared by the CLI and the GUI.
    - on_log(line): receives progress/log lines (CLI passes print; GUI passes a
      thread-safe marshaller). Defaults to a no-op.
    - stop_event: threading.Event; setting it stops playback early (GUI stop
      button / CLI ENTER). None plays until the track ends, then holds position
      until Ctrl+C.
    - notice: optional line logged right after the track summary (CLI uses it
      for the "press ENTER / Ctrl+C" hint).
    Validation failures raise ValueError; device/prep failures raise RuntimeError
    (so the GUI can catch and show a dialog instead of exiting).
    On return, real location has been restored.
    """
    if on_log is None:
        on_log = lambda line: None
    if speed is not None and speed <= 0:
        raise ValueError('error: --speed must be > 0')
    if scale is not None and scale <= 0:
        raise ValueError('error: --scale must be > 0')
    if repeat < 1:
        raise ValueError('error: --repeat must be >= 1')
    if duration is not None and duration <= 0:
        raise ValueError('error: --duration must be > 0')

    udid = pick_device(udid)
    try:
        points = trackprep.prepare(input_path, speed, scale, repeat, from_gcj02)
    except SystemExit as e:
        raise RuntimeError(str(e)) from None

    if output:
        trackprep.write_gpx(output, points, Path(input_path).stem)
    dist = sum(trackprep.haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
    dur = (points[-1][2] - points[0][2]).total_seconds()
    on_log(f'device {udid} | {len(points)} points | {dist / 1000:.2f} km | {dur:.0f} s track')
    if notice:
        on_log(notice)

    keepalive = float(config.load_config().get('keepalive_interval_s', 1.0))
    logger, handler = _forward_pmd_logs(on_log)
    ok = False
    try:
        asyncio.run(_play_async(points, udid, duration, stop_event, on_log, keepalive))
        ok = True
    except KeyboardInterrupt:
        pass
    finally:
        logger.removeHandler(handler)
        if not ok:
            # Playback was interrupted before its own clear ran (e.g. Ctrl+C or
            # a mid-session error): guarantee real location is restored.
            restore(udid)
    on_log('done. real location restored.')


def cmd_play(args):
    speed = args.speed
    if args.preset:
        preset = config.find_preset(args.preset)
        if preset is None:
            names = ', '.join(str(p.get('label', '')) for p in config.speed_presets())
            sys.exit(f'error: unknown preset {args.preset!r}. Available: {names}')
        speed = config.preset_speed_mps(preset)

    interactive = not args.duration and sys.stdin.isatty()
    if args.duration:
        notice = f'playing for {args.duration:g}s, then auto-restoring real location...'
    elif interactive:
        notice = 'playing... press ENTER to stop and restore real location.'
    else:
        notice = 'playing... press Ctrl+C to stop and restore real location.'

    stop_event = None
    if interactive:
        stop_event = threading.Event()

        def watch_enter():
            try:
                input()
            except (EOFError, OSError):
                pass
            stop_event.set()
        threading.Thread(target=watch_enter, daemon=True).start()
    elif args.duration:
        stop_event = threading.Event()

    try:
        play_track(args.input, speed=speed, scale=args.scale, repeat=args.repeat,
                   from_gcj02=args.from_gcj02, udid=args.udid, duration=args.duration,
                   output=args.output, stop_event=stop_event, on_log=print, notice=notice)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))


def cmd_clear(args):
    try:
        udid = pick_device(args.udid)
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    restore(udid)
    print('real location restored.')


def cmd_devices(args):
    try:
        devs = list_devices()
    except (ValueError, RuntimeError) as e:
        sys.exit(str(e))
    if not devs:
        print('no iPhone detected.')
        return
    for d in devs:
        print(f"{d.get('Identifier', '?')}  {d.get('DeviceName', '')}  "
              f"{d.get('ProductType', '')}  iOS {d.get('ProductVersion', '')}  {d.get('ConnectionType', '')}")


def cmd_gui(args):
    import trackplayer_gui
    trackplayer_gui.main()


def main():
    ap = argparse.ArgumentParser(
        prog='trackplayer',
        description='Simulate iPhone GPS along a GPX/KML track on Windows; auto-restores real location on exit.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  trackplayer play route.kml --speed 3 --repeat 2\n'
               '  trackplayer play route.gpx --preset 跑步\n'
               '  trackplayer play amap.gpx --from-gcj02 --speed 3\n'
               '  trackplayer play loop.gpx --speed 5 --duration 60\n'
               '  trackplayer devices\n'
               '  trackplayer clear\n'
               '  trackplayer gui\n'
               '\n'
               'Requires iTunes (usbmux service) + an iPhone with Developer Mode enabled, USB-connected.\n'
               'Only GPS coordinates are simulated, not pedometer/motion sensors.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('play', help='prepare a track and play it on the device')
    p.add_argument('input', help='input .gpx or .kml file')
    p.add_argument('-o', '--output', help='keep the prepared .gpx at this path (default: not written)')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--speed', type=float, metavar='MPS', help='constant speed in m/s, resampled to 1 Hz (e.g. 3 = running)')
    g.add_argument('--scale', type=float, metavar='X', help='speed multiplier vs recorded timestamps (e.g. 2 = twice as fast)')
    g.add_argument('--preset', metavar='NAME', help='speed preset from config (e.g. 步行/跑步/骑行/驾车); sets speed in m/s')
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

    gu = sub.add_parser('gui', help='launch the graphical interface (same as trackplayer-gui)')
    gu.set_defaults(func=cmd_gui)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()