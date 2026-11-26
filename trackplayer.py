#!/usr/bin/env python3
"""Track Player — one-command iPhone GPS track simulation on Windows.

Wraps trackprep (preprocessing) + pymobiledevice3 (DVT LocationSimulation):
prepares a GPX/KML/GeoJSON track, auto-detects the device, plays it over a
persistent DVT location channel that is held open for the whole session, and
restores real location automatically on exit (ENTER / Ctrl+C / --duration /
error). While playing, the current coordinate is re-asserted on a keep-alive
interval to resist iOS fusion-location snap-back; after the track ends the last
position is held until stopped. Playback can be paused (position frozen and
held, duration clock frozen) and resumed from the paused point.

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


async def _play_async(points, udid, duration, stop_event, on_log, keepalive, on_point=None,
                      pause_event=None):
    loop = asyncio.get_running_loop()
    end = loop.time() + duration if duration else None

    def stop_set():
        return stop_event is not None and stop_event.is_set()

    def paused():
        return pause_event is not None and pause_event.is_set()

    def stopped():
        if stop_set():
            return True
        # The --duration clock freezes while paused (pause_gate shifts `end`).
        if end is None or paused():
            return False
        return loop.time() >= end

    async with _location_channel(udid) as ls:
        last = None

        async def pause_gate():
            """Freeze track progression while paused, holding the current coordinate.

            Re-asserts the last position on the keep-alive interval so iOS fusion
            location cannot snap back during a pause. Returns the seconds spent
            paused so callers shift their own deadlines; `end` is shifted here.
            """
            nonlocal end
            if not paused():
                return 0.0
            t0 = loop.time()
            on_log('paused; holding current position (keep-alive)...')
            while paused() and not stop_set():
                if last is not None:
                    await ls.set(last[0], last[1])
                    if on_point is not None:
                        on_point(last[0], last[1])
                deadline = loop.time() + keepalive
                while loop.time() < deadline and paused() and not stop_set():
                    await asyncio.sleep(0.1)
            spent = loop.time() - t0
            if end is not None:
                end += spent
            if not stop_set():
                on_log('resumed.')
            return spent

        async def nap(seconds):
            # Sleep in small slices so stop/pause/duration react promptly.
            deadline = loop.time() + seconds
            while not stopped():
                deadline += await pause_gate()
                if stopped():
                    return
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return
                await asyncio.sleep(min(0.1, remaining))

        try:
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
                await pause_gate()
                if stopped():
                    break
                await ls.set(lat, lon)
                on_log(f'set location to {lat:.6f} {lon:.6f}')
                if on_point is not None:
                    on_point(lat, lon)
                last = (lat, lon)
                prev_t = t
            # Track finished: hold the last position, re-asserting it on the
            # keep-alive interval until stopped (also resists snap-back at rest).
            if last is not None and not stopped():
                on_log(f'track finished; holding {last[0]:.6f} {last[1]:.6f} '
                       f'(keep-alive every {keepalive:g}s)...')
                while not stopped():
                    await ls.set(last[0], last[1])
                    if on_point is not None:
                        on_point(last[0], last[1])
                    await nap(keepalive)
        finally:
            on_log('restoring real location...')
            with suppress(Exception):
                await ls.clear()


def play_track(input_path, *, speed=None, scale=None, repeat=1, from_gcj02=False,
               udid=None, duration=None, output=None, script=None,
               stop_event=None, on_log=None, notice=None, on_point=None, pause_event=None):
    """Prepare a track, play it on the device, and restore real location on exit.

    UI-agnostic core shared by the CLI, the GUI and the Web UI.
    - on_log(line): receives progress/log lines (CLI passes print; GUI passes a
      thread-safe marshaller). Defaults to a no-op.
    - stop_event: threading.Event; setting it stops playback early (GUI stop
      button / CLI ENTER). None plays until the track ends, then holds position
      until Ctrl+C.
    - pause_event: threading.Event; while set, track progression freezes and the
      current coordinate is held (keep-alive re-asserts resist snap-back). The
      --duration clock freezes too. Clearing it resumes from the paused point.
    - notice: optional line logged right after the track summary (CLI uses it
      for the "press ENTER / Ctrl+C" hint).
    - on_point(lat, lon): called with each injected WGS-84 coordinate (feeding,
      pause hold and keep-alive hold), for live map previews. Defaults to a no-op.
    - script: optional movement-script leg list (per-leg speeds + stay points);
      compiled by trackprep and mutually exclusive with speed/scale.
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
        points = trackprep.prepare(input_path, speed, scale, repeat, from_gcj02, script)
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
        asyncio.run(_play_async(points, udid, duration, stop_event, on_log, keepalive, on_point,
                                pause_event))
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
    script = None
    if args.script:
        try:
            with open(args.script, encoding='utf-8') as f:
                script = json.load(f)
        except (OSError, ValueError) as e:
            sys.exit(f'error: cannot read script {args.script}: {e}')
        if not isinstance(script, list) or not script:
            sys.exit('error: script must be a non-empty list of legs')
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
        notice = 'playing... ENTER stops, p+ENTER pauses/resumes (position held while paused).'
    else:
        notice = 'playing... press Ctrl+C to stop and restore real location.'

    stop_event = None
    pause_event = None
    if interactive:
        stop_event = threading.Event()
        pause_event = threading.Event()

        def watch_keys():
            while True:
                try:
                    line = input()
                except (EOFError, OSError):
                    break
                if line.strip().lower() in ('p', 'pause', 'resume'):
                    if pause_event.is_set():
                        pause_event.clear()
                    else:
                        pause_event.set()
                else:
                    break
            stop_event.set()
        threading.Thread(target=watch_keys, daemon=True).start()
    elif args.duration:
        stop_event = threading.Event()

    try:
        play_track(args.input, speed=speed, scale=args.scale, repeat=args.repeat,
                   from_gcj02=args.from_gcj02, udid=args.udid, duration=args.duration,
                   output=args.output, script=script, stop_event=stop_event, on_log=print,
                   notice=notice, pause_event=pause_event)
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


def cmd_web(args):
    import trackplayer_web
    trackplayer_web.serve(port=args.port, open_browser=not args.no_open)


def main():
    ap = argparse.ArgumentParser(
        prog='trackplayer',
        description='Simulate iPhone GPS along a GPX/KML track on Windows; auto-restores real location on exit.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='Examples:\n'
               '  trackplayer web                     # preferred: browser map UI\n'
               '  trackplayer play route.kml --speed 3 --repeat 2\n'
               '  trackplayer play route.gpx --preset 跑步\n'
               '  trackplayer play amap.gpx --from-gcj02 --speed 3\n'
               '  trackplayer play loop.gpx --speed 5 --duration 60\n'
               '  trackplayer play loop.gpx --script examples/legs_demo.json\n'
               '  trackplayer devices\n'
               '  trackplayer clear\n'
               '  trackplayer gui\n'
               '\n'
               'Requires iTunes (usbmux service) + an iPhone with Developer Mode enabled, USB-connected.\n'
               'Only GPS coordinates are simulated, not pedometer/motion sensors.')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('play', help='prepare a track and play it on the device')
    p.add_argument('input', help='input .gpx / .kml / .geojson / .json file')
    p.add_argument('-o', '--output', help='keep the prepared .gpx at this path (default: not written)')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--speed', type=float, metavar='MPS', help='constant speed in m/s, resampled to 1 Hz (e.g. 3 = running)')
    g.add_argument('--scale', type=float, metavar='X', help='speed multiplier vs recorded timestamps (e.g. 2 = twice as fast)')
    g.add_argument('--preset', metavar='NAME', help='speed preset from config (e.g. 步行/跑步/骑行/驾车); sets speed in m/s')
    g.add_argument('--script', metavar='FILE', help='JSON movement script (per-leg speeds + stay points); see examples/legs_demo.json')
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

    w = sub.add_parser('web', help='launch the browser map UI (preferred; local server + live map preview)')
    w.add_argument('--port', type=int, default=8799, help='local port to bind (default 8799)')
    w.add_argument('--no-open', action='store_true', help='do not auto-open the browser')
    w.set_defaults(func=cmd_web)

    gu = sub.add_parser('gui', help='launch the graphical interface (same as trackplayer-gui)')
    gu.set_defaults(func=cmd_gui)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
