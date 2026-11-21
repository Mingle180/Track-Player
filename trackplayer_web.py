#!/usr/bin/env python3
"""Track Player Web UI — local map preview + playback control in the browser.

A stdlib-only local web server (http.server + Server-Sent Events) that serves a
Leaflet map on AMap (GCJ-02) tiles and drives the same play_track core as the CLI
and Tkinter GUI. Injected coordinates are always WGS-84; for display on the
GCJ-02 basemap they are converted with trackprep.wgs84_to_gcj02 so the preview
matches what an AMap-SDK app shows.

Run with ``trackplayer web`` (binds 127.0.0.1 only). No third-party web framework
is required; Leaflet is loaded from a CDN by the page.
"""

import argparse
import json
import os
import queue
import tempfile
import threading
import webbrowser
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import trackprep
import trackplayer as tp
import trackplayer_config as config

MAX_PREVIEW_POINTS = 2000


class _HTTPServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port and
    # silently split requests between instances (a play on one, stop on the
    # other leaves a hidden keep-alive holder). Refuse reuse there so a
    # duplicate instance fails fast with the friendly bind error instead.
    allow_reuse_address = os.name != 'nt'


class Hub:
    """Fan-out playback events to all connected SSE subscribers."""

    def __init__(self):
        self._subs = []
        self._lock = threading.Lock()

    def subscribe(self):
        q = queue.Queue()
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            q.put(event)


class State:
    def __init__(self):
        self.hub = Hub()
        self.lock = threading.Lock()
        self.upload_path = None
        self.upload_name = None
        self.playing = False
        self.last_point = None
        self.stop_event = None
        self.pause_event = None

    def set_upload(self, path, name):
        with self.lock:
            old = self.upload_path
            self.upload_path = path
            self.upload_name = name
        if old and old != path:
            try:
                Path(old).unlink()
            except OSError:
                pass

    def cleanup(self):
        with self.lock:
            path = self.upload_path
            self.upload_path = None
        if path:
            try:
                Path(path).unlink()
            except OSError:
                pass


def _resolve_speed(params):
    """Return (speed, scale) from request params; preset wins like the CLI."""
    preset = params.get('preset')
    if preset:
        p = config.find_preset(preset)
        if p is None:
            raise ValueError(f'unknown preset {preset!r}')
        return config.preset_speed_mps(p), None
    speed = params.get('speed')
    scale = params.get('scale')
    return (float(speed) if speed else None), (float(scale) if scale else None)


def _prepare_params(params):
    speed, scale = _resolve_speed(params)
    return {
        'speed': speed,
        'scale': scale,
        'repeat': int(params.get('repeat') or 1),
        'from_gcj02': str(params.get('from_gcj02', '')).lower() in ('1', 'true', 'on', 'yes'),
        'udid': params.get('udid') or None,
        'duration': float(params['duration']) if params.get('duration') else None,
    }


def _summary(points):
    dist = sum(trackprep.haversine(a[0], a[1], b[0], b[1]) for a, b in zip(points, points[1:]))
    dur = (points[-1][2] - points[0][2]).total_seconds()
    return {'n': len(points), 'km': round(dist / 1000, 3), 'dur_s': round(dur)}


def _play_worker(state, params):
    def on_log(line):
        state.hub.publish({'type': 'log', 'line': line})

    def on_point(lat, lon):
        glat, glon = trackprep.wgs84_to_gcj02(lat, lon)
        with state.lock:
            state.last_point = (glat, glon)
        state.hub.publish({'type': 'point', 'lat': glat, 'lon': glon})

    stop = threading.Event()
    pause = threading.Event()
    with state.lock:
        state.stop_event = stop
        state.pause_event = pause
        state.playing = True
    state.hub.publish({'type': 'state', 'state': 'playing'})
    try:
        tp.play_track(state.upload_path, stop_event=stop, pause_event=pause, on_log=on_log,
                      on_point=on_point, **params)
    except (ValueError, RuntimeError) as e:
        state.hub.publish({'type': 'log', 'line': str(e)})
    finally:
        with state.lock:
            state.playing = False
            state.stop_event = None
            state.pause_event = None
        state.hub.publish({'type': 'state', 'state': 'idle'})


def _make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        # -- helpers --
        def _json(self, obj, code=200):
            body = json.dumps(obj).encode('utf-8')
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _query(self):
            raw = parse_qs(urlparse(self.path).query)
            return {k: v[0] for k, v in raw.items()}

        def _body(self):
            n = int(self.headers.get('Content-Length', 0))
            self._body_read = True
            return self.rfile.read(n) if n else b''

        def _drain(self):
            # keep-alive 下提前报错时也要读完请求体，否则残留字节会被当成下一个请求行
            if getattr(self, '_body_read', False):
                return
            n = int(self.headers.get('Content-Length', 0))
            if n:
                self.rfile.read(n)

        def _json_body(self):
            raw = self._body()
            return json.loads(raw) if raw else {}

        # -- routes --
        def do_GET(self):
            path = urlparse(self.path).path
            try:
                if path == '/':
                    self._html(INDEX_HTML)
                elif path == '/api/devices':
                    with state.lock:
                        lp = state.last_point
                    self._json({'devices': _devices(),
                                'last_point': list(lp) if lp else None})
                elif path == '/api/config':
                    self._json(_config_payload())
                elif path == '/api/preview':
                    self._preview()
                elif path == '/api/stream':
                    self._sse()
                else:
                    self._json({'error': 'not found'}, 404)
            except (ValueError, RuntimeError) as e:
                self._json({'error': str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                self._json({'error': str(e)}, 500)

        def do_POST(self):
            path = urlparse(self.path).path
            self._body_read = False
            try:
                if path == '/api/track':
                    self._upload()
                elif path == '/api/play':
                    self._play()
                elif path == '/api/stop':
                    self._stop()
                elif path == '/api/pause':
                    self._pause()
                elif path == '/api/drawn':
                    self._drawn()
                elif path == '/api/clear':
                    self._clear()
                else:
                    self._json({'error': 'not found'}, 404)
            except (ValueError, RuntimeError) as e:
                self._drain()
                self._json({'error': str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                self._drain()
                self._json({'error': str(e)}, 500)

        def _html(self, text):
            body = text.encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _upload(self):
            name = self._query().get('name', 'track.gpx')
            suffix = Path(name).suffix.lower()
            if suffix not in ('.gpx', '.kml', '.geojson', '.json'):
                raise ValueError('only .gpx / .kml / .geojson / .json files are supported')
            data = self._body()
            if not data:
                raise ValueError('empty file')
            fd, tmp = tempfile.mkstemp(suffix=suffix, prefix='trackplayer_web_')
            with open(fd, 'wb') as f:
                f.write(data)
            state.set_upload(tmp, name)
            self._json({'ok': True, 'name': name})

        def _preview(self):
            if not state.upload_path:
                raise ValueError('no track uploaded yet')
            params = _prepare_params(self._query())
            points = trackprep.prepare(state.upload_path, params['speed'], params['scale'],
                                       params['repeat'], params['from_gcj02'])
            disp = [trackprep.wgs84_to_gcj02(lat, lon) for lat, lon, _ in points]
            if len(disp) > MAX_PREVIEW_POINTS:
                step = len(disp) / MAX_PREVIEW_POINTS
                disp = [disp[int(i * step)] for i in range(MAX_PREVIEW_POINTS)] + [disp[-1]]
            self._json({'name': state.upload_name, 'points': disp, 'summary': _summary(points)})

        def _play(self):
            if not state.upload_path:
                raise ValueError('no track uploaded yet')
            with state.lock:
                if state.playing:
                    raise ValueError('already playing')
            params = _prepare_params(self._json_body())
            threading.Thread(target=_play_worker, args=(state, params), daemon=True).start()
            self._json({'ok': True})

        def _stop(self):
            with state.lock:
                ev = state.stop_event
            if ev:
                ev.set()
            self._json({'ok': True})

        def _pause(self):
            want = bool(self._json_body().get('paused'))
            with state.lock:
                ev = state.pause_event
                playing = state.playing
            if not playing or ev is None:
                raise ValueError('not playing')
            if want:
                ev.set()
            else:
                ev.clear()
            state.hub.publish({'type': 'state', 'state': 'paused' if want else 'playing'})
            self._json({'ok': True, 'paused': want})

        def _drawn(self):
            raw = self._json_body().get('points') or []
            if len(raw) < 2:
                raise ValueError('draw at least 2 points on the map first')
            wgs = [trackprep.gcj02_to_wgs84(float(p[0]), float(p[1])) for p in raw]
            points = [(lat, lon, trackprep.BASE_TIME + timedelta(seconds=i))
                      for i, (lat, lon) in enumerate(wgs)]
            fd, tmp = tempfile.mkstemp(suffix='.gpx', prefix='trackplayer_drawn_')
            os.close(fd)
            trackprep.write_gpx(tmp, points, 'drawn track')
            state.set_upload(tmp, 'drawn track.gpx')
            self._json({'ok': True, 'name': 'drawn track.gpx', 'summary': _summary(points)})

        def _clear(self):
            with state.lock:
                if state.playing:
                    raise ValueError('stop playback before clearing')
            udid = tp.pick_device(self._json_body().get('udid'))
            tp.restore(udid)
            state.hub.publish({'type': 'log', 'line': 'real location restored.'})
            self._json({'ok': True})

        def _sse(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'keep-alive')
            self.end_headers()
            q = state.hub.subscribe()

            def send(ev):
                self.wfile.write(f'data: {json.dumps(ev)}\n\n'.encode('utf-8'))
                self.wfile.flush()

            try:
                with state.lock:
                    playing = state.playing
                    paused = state.pause_event is not None and state.pause_event.is_set()
                send({'type': 'state',
                      'state': ('paused' if paused else 'playing') if playing else 'idle'})
                while True:
                    try:
                        ev = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b': keepalive\n\n')
                        self.wfile.flush()
                        continue
                    send(ev)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                state.hub.unsubscribe(q)

    return Handler


def _devices():
    out = []
    for d in tp.list_devices():
        out.append({
            'udid': d.get('Identifier', ''),
            'name': d.get('DeviceName', ''),
            'product': d.get('ProductType', ''),
            'ios': d.get('ProductVersion', ''),
            'connection': d.get('ConnectionType', ''),
        })
    return out


def _config_payload():
    cfg = config.load_config()
    return {
        'presets': config.speed_presets(cfg),
        'default_speed_mps': cfg.get('default_speed_mps', 3.0),
        'default_scale': cfg.get('default_scale', 2.0),
        'default_repeat': cfg.get('default_repeat', 1),
    }


def serve(port=8799, open_browser=True):
    state = State()
    try:
        server = _HTTPServer(('127.0.0.1', port), _make_handler(state))
    except OSError as e:
        raise SystemExit(f'error: cannot bind 127.0.0.1:{port} ({e.strerror or e}). '
                         f'Pick a free port with --port, e.g. --port 8801.')
    url = f'http://127.0.0.1:{port}/'
    print(f'Track Player Web UI: {url}  (Ctrl+C to stop)')
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nshutting down...')
    finally:
        with state.lock:
            ev = state.stop_event
        if ev:
            ev.set()
        server.shutdown()
        server.server_close()
        state.cleanup()


def main():
    ap = argparse.ArgumentParser(prog='trackplayer-web',
                                 description='Track Player browser map UI (local server + live map preview).')
    ap.add_argument('--port', type=int, default=8799, help='local port to bind (default 8799)')
    ap.add_argument('--no-open', action='store_true', help='do not auto-open the browser')
    args = ap.parse_args()
    serve(port=args.port, open_browser=not args.no_open)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Track Player</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.css">
<script src="https://cdn.jsdelivr.net/npm/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
(function(){
  var d = document.documentElement;
  d.dataset.theme = localStorage.getItem('tp-theme') || (matchMedia('(prefers-color-scheme:dark)').matches ? 'dark' : 'light');
  d.dataset.font = localStorage.getItem('tp-font') || 'default';
  if (localStorage.getItem('tp-map-dim') === '1') d.dataset.mapDim = '1';
})();
</script>
<style>
:root{
  --bg-page:#FFFFFF; --bg-sidebar:#F7F6F3; --bg-hover:#EFEDEA; --bg-elevated:#FFFFFF;
  --text:#37352F; --text-2:#6B6A67; --text-3:#9B9A97;
  --border:#E9E9E7; --border-strong:#D3D1CB; --divider:#EDECEB;
  --accent:#2383E2; --link:#1A6FB5; --ink:#37352F; --on-ink:#FFFFFF;
  --success:#448361; --warning:#946300; --danger:#C4322C; --draw:#D9730D;
  --chip-idle-bg:#F1F0EE;  --chip-idle-fg:#6B6A67;  --chip-idle-dot:#787774;
  --chip-play-bg:#EBF5EE;  --chip-play-fg:#2F6B45;  --chip-play-dot:#448361;
  --chip-pause-bg:#FBF3E4; --chip-pause-fg:#8A5B00; --chip-pause-dot:#946300;
  --chip-hold-bg:#EEF4FB;  --chip-hold-fg:#1A5F9E;  --chip-hold-dot:#2383E2;
  --chip-err-bg:#FCEDED;   --chip-err-fg:#B02A24;   --chip-err-dot:#C4322C;
  --callout-info:#F1F7FC; --callout-ok:#F0F7F2; --callout-warn:#FBF5EC; --callout-err:#FDF2F2;
  --r-sm:3px; --r-md:4px; --r-block:6px; --r-map:8px; --r-pop:12px;
  --shadow-pop:0 0 0 1px rgba(15,15,15,.04),0 3px 6px rgba(15,15,15,.10),0 9px 24px rgba(15,15,15,.20);
  --t-fast:120ms; --t-mid:180ms; --ease:cubic-bezier(.2,0,0,1);
  --font-ui:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  --font-serif:Georgia,"Songti SC","SimSun",serif;
  --font-mono:ui-monospace,Consolas,"Courier New",monospace;
  --font-body:var(--font-ui);
  --sidebar-w:240px;
}
html[data-theme="dark"]{
  --bg-page:#191919; --bg-sidebar:#202020; --bg-hover:#2A2A2A; --bg-elevated:#2F2F2F;
  --text:#D4D4D4; --text-2:#929292; --text-3:#6F6F6F;
  --border:#393939; --border-strong:#4A4A4A; --divider:#2F2F2F;
  --accent:#529CCA; --link:#529CCA; --ink:#D4D4D4; --on-ink:#191919;
  --success:#529E72; --warning:#D9A441; --danger:#FF7369; --draw:#D9A441;
  --chip-idle-bg:#2A2A2A;  --chip-idle-fg:#A5A5A5;  --chip-idle-dot:#838383;
  --chip-play-bg:#1F3329;  --chip-play-fg:#7FD3A3;  --chip-play-dot:#529E72;
  --chip-pause-bg:#3A2E17; --chip-pause-fg:#E5C07B; --chip-pause-dot:#D9A441;
  --chip-hold-bg:#1C2C3D;  --chip-hold-fg:#8FC4EE;  --chip-hold-dot:#529CCA;
  --chip-err-bg:#3A2220;   --chip-err-fg:#FF9C94;   --chip-err-dot:#FF7369;
  --callout-info:#1C2C3D; --callout-ok:#1F3329; --callout-warn:#3A2E17; --callout-err:#3A2220;
}
html[data-theme="dark"] .leaflet-tile-pane{filter:invert(1) hue-rotate(180deg) brightness(.95) contrast(.9) saturate(.7)}
html[data-theme="dark"][data-map-dim="1"] .leaflet-tile-pane{filter:none;opacity:.85}
html[data-theme="dark"] .tp-pop{--text-2:#9B9B9B}
html[data-font="serif"]{--font-body:var(--font-serif)}
html[data-font="mono"]{--font-body:var(--font-mono)}

*{box-sizing:border-box}
[hidden]{display:none!important}
html{scroll-behavior:smooth}
body{margin:0;font-family:var(--font-body);font-size:.875rem;line-height:1.5;color:var(--text);background:var(--bg-page)}
#app{display:flex;min-height:100vh}
#side{width:var(--sidebar-w);min-width:var(--sidebar-w);background:var(--bg-sidebar);border-right:1px solid var(--border);
      padding:8px;display:flex;flex-direction:column;gap:2px;position:sticky;top:0;height:100vh;overflow-y:auto}
#page{flex:1;min-width:0;padding:32px 48px 96px;max-width:1120px;margin:0 auto}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:var(--r-md)}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}
a{color:var(--link)}

/* 侧栏 */
.side-brand{display:flex;align-items:center;gap:8px;padding:8px;font-weight:600;font-size:.875rem}
.side-sec{font-size:.6875rem;color:var(--text-2);padding:12px 8px 4px;font-weight:500}
.side-item{display:flex;align-items:center;gap:8px;height:28px;padding:0 8px;border-radius:var(--r-md);
           color:var(--text);text-decoration:none;font-size:.8125rem;cursor:pointer;
           border-left:2px solid transparent;transition:background var(--t-fast) var(--ease)}
.side-item:hover{background:var(--bg-hover)}
.side-item.active{background:var(--bg-hover);border-left-color:var(--accent);font-weight:500}
button.side-item{width:100%;background:transparent;border:0;border-left:2px solid transparent;font-weight:400;justify-content:flex-start}
button.side-item.active{font-weight:500}
.side-spacer{flex:1}
.device-card{margin:8px;padding:10px;background:var(--bg-page);border:1px solid var(--border);border-radius:var(--r-block);
             font-size:.75rem;color:var(--text-2)}
.device-card b{display:block;font-size:.8125rem;color:var(--text);font-weight:600;margin-bottom:2px}
.device-card b::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--text-2);margin-right:6px;vertical-align:middle}
.device-card.online b::before{background:var(--success)}
.device-card code{font-family:var(--font-mono);font-size:.6875rem;word-break:break-all}
.device-card .ud{display:flex;align-items:center;gap:4px;margin-top:4px}
.device-card .ud code{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* 页头 */
.page-hd{display:flex;align-items:flex-start;gap:12px;padding-bottom:12px;border-bottom:1px solid var(--divider)}
.page-icon{font-size:2rem;line-height:1}
#page-title{font-size:1.875rem;font-weight:700;line-height:1.2;margin:0}
#page-sub{font-size:.8125rem;color:var(--text-2);margin-top:4px}
.page-hd .chip{margin-left:auto;margin-top:6px}

/* 属性行 */
.prop{display:grid;grid-template-columns:96px 1fr;align-items:center;min-height:32px;gap:8px;padding:2px 6px;
      border-radius:var(--r-md);transition:background var(--t-fast) var(--ease)}
.prop:hover{background:var(--bg-hover)}
.prop > .k{font-size:.8125rem;color:var(--text-2)}
.prop > .k label{cursor:pointer}
.prop .v{display:flex;align-items:center;gap:6px;min-width:0;flex-wrap:wrap}
.prop select,.prop input[type=number]{border:1px solid transparent;background:transparent;color:var(--text);
      font:inherit;padding:4px 6px;border-radius:var(--r-md);min-height:28px}
.prop select{flex:1;min-width:0;max-width:280px}
.prop input[type=number]{width:80px}
.prop select:hover,.prop input[type=number]:hover{background:var(--bg-sidebar);border-color:var(--border)}
.prop select:focus,.prop input[type=number]:focus{background:var(--bg-page);border-color:var(--accent)}
.prop input::placeholder{color:var(--text-3)}
input[type=number]::-webkit-outer-spin-button,input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
input[type=number]{-moz-appearance:textfield;appearance:textfield}
.unit{font-size:.75rem;color:var(--text-2)}
#summary{font-size:.8125rem;color:var(--text-2)}

/* 状态 chip */
.chip{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 8px;border-radius:var(--r-sm);
      font-size:.75rem;font-weight:500;white-space:nowrap;background:var(--chip-idle-bg);color:var(--chip-idle-fg)}
.chip::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--chip-idle-dot);flex:none}
.chip.playing{background:var(--chip-play-bg);color:var(--chip-play-fg)}   .chip.playing::before{background:var(--chip-play-dot)}
.chip.paused{background:var(--chip-pause-bg);color:var(--chip-pause-fg)}   .chip.paused::before{background:var(--chip-pause-dot)}
.chip.holding{background:var(--chip-hold-bg);color:var(--chip-hold-fg)}    .chip.holding::before{background:var(--chip-hold-dot)}
.chip.error{background:var(--chip-err-bg);color:var(--chip-err-fg)}        .chip.error::before{background:var(--chip-err-dot)}

/* 按钮 */
button{font:inherit;font-size:.8125rem;font-weight:500;height:32px;padding:0 12px;cursor:pointer;
       border:1px solid var(--border);border-radius:var(--r-md);background:var(--bg-sidebar);color:var(--text);
       transition:background var(--t-fast) var(--ease)}
button:hover:not(:disabled){background:var(--bg-hover)}
button.primary{background:var(--ink);color:var(--on-ink);border-color:var(--ink)}
button.primary:hover:not(:disabled){opacity:.88;background:var(--ink)}
button.danger{background:transparent;color:var(--danger);border-color:var(--danger)}
button.danger:hover:not(:disabled){background:var(--callout-err)}
button.ghost{background:transparent;border-color:transparent}
button.icon{width:28px;height:28px;padding:0;display:inline-flex;align-items:center;justify-content:center;flex:none}
button:disabled{opacity:.4;cursor:not-allowed}
button[aria-busy="true"]{position:relative;color:transparent}
button[aria-busy="true"]::after{content:"";position:absolute;inset:0;margin:auto;width:14px;height:14px;
       border:2px solid var(--text-2);border-top-color:transparent;border-radius:50%;animation:spin .7s linear infinite}
button.primary[aria-busy="true"]::after{border-color:var(--on-ink);border-top-color:transparent}
@keyframes spin{to{transform:rotate(360deg)}}

/* switch（原生 checkbox 画成开关，保留 .checked 语义） */
input.switch{appearance:none;-webkit-appearance:none;position:relative;width:28px;height:16px;flex:none;margin:0;
      border-radius:8px;background:var(--border-strong);border:0;padding:0;cursor:pointer;
      transition:background var(--t-fast) var(--ease)}
input.switch::after{content:"";position:absolute;top:2px;left:2px;width:12px;height:12px;border-radius:50%;
      background:#fff;transition:transform var(--t-fast) var(--ease)}
input.switch:checked{background:var(--accent)}
input.switch:checked::after{transform:translateX(12px)}
.sw-row{display:inline-flex;align-items:center;gap:8px;font-size:.8125rem}

/* segmented */
.seg{display:inline-flex;padding:2px;background:var(--bg-sidebar);border-radius:var(--r-md);gap:2px}
.seg button{height:24px;border:1px solid transparent;background:transparent;font-size:.75rem;padding:0 10px}
.seg button[aria-selected="true"],.seg button[aria-pressed="true"]{background:var(--bg-page);border-color:var(--border);font-weight:500}

/* callout / 块 / toggle */
#callout-root{display:flex;flex-direction:column;gap:8px;margin-top:12px}
#callout-root:empty{margin-top:0}
.callout{display:flex;gap:10px;padding:12px;border-radius:var(--r-block);background:var(--callout-info);font-size:.875rem;align-items:flex-start}
.callout.warn{background:var(--callout-warn)} .callout.err{background:var(--callout-err)}
.callout.ok{background:var(--callout-ok)}   .callout.gray{background:var(--bg-sidebar)}
.callout .ci{flex:none;line-height:1.4}
.callout .cb{min-width:0;flex:1}
.callout ol{margin:4px 0 0;padding-left:18px}
.callout .ca{margin-top:8px;display:flex;gap:8px}
.callout button{height:28px}
.block{border-radius:var(--r-block);margin-top:16px;scroll-margin-top:16px}
.block-hd{display:flex;align-items:center;gap:8px;font-size:.875rem;font-weight:600;padding:6px}
.block-hd .sp{flex:1}
.toggle-hd{display:flex;align-items:center;gap:6px;padding:6px;cursor:pointer;font-weight:600;font-size:.875rem;
           border-radius:var(--r-md);background:none;border:0;height:auto;width:100%;text-align:left;color:var(--text)}
.toggle-hd:hover{background:var(--bg-hover)}
.toggle-hd .caret{transition:transform var(--t-mid) var(--ease);color:var(--text-2);display:inline-block}
.toggle-hd[aria-expanded="true"] .caret{transform:rotate(90deg)}
.toggle-bd{overflow:hidden;padding:2px 0 6px;transition:height var(--t-mid) var(--ease),opacity var(--t-mid) var(--ease)}

/* 文件块 */
.file-block{position:relative;display:flex;align-items:center;gap:10px;padding:12px;background:var(--bg-sidebar);
            border:1px solid var(--border);border-radius:var(--r-block);cursor:pointer;flex:1;min-width:0;
            transition:background var(--t-fast) var(--ease)}
.file-block:hover{background:var(--bg-hover)}
.file-block:focus-within{box-shadow:0 0 0 2px var(--accent)}
.file-block.drag{box-shadow:0 0 0 2px var(--accent)}
.file-block .t{font-size:.875rem;font-weight:500;display:block}
.file-block .s{font-size:.75rem;color:var(--text-2);display:block}
.file-block .fi{font-size:1.25rem;line-height:1;flex:none}
#file{position:absolute;width:1px;height:1px;opacity:0;left:0;top:0}

/* 地图 */
#map-wrap{position:relative;border:1px solid var(--border);border-radius:var(--r-map);overflow:hidden}
/* Leaflet 面板自带 z-index 200–1000；#map 必须自成层叠上下文，否则瓦片会盖住兄弟覆盖层 */
#map{position:relative;z-index:0;height:clamp(360px,52vh,640px);background:var(--bg-sidebar)}
#map-toolbar{position:absolute;top:10px;right:10px;z-index:5;display:flex;gap:4px;padding:4px;align-items:center;
             background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--r-block);box-shadow:var(--shadow-pop)}
#map-toolbar button{height:28px}
#map-wrap.drawing{box-shadow:0 0 0 2px var(--accent)}
#map-wrap.drawing #map{cursor:crosshair}
#draw-badge{position:absolute;top:10px;left:10px;z-index:5;padding:6px 10px;font-size:.75rem;
            background:var(--link);color:#fff;border-radius:var(--r-md);box-shadow:var(--shadow-pop)}
html[data-theme="dark"] #draw-badge{background:var(--accent);color:var(--bg-page)}
#map-empty{position:absolute;inset:0;z-index:4;display:flex;align-items:center;justify-content:center;pointer-events:none}
#map-empty .ec{display:flex;flex-direction:column;align-items:center;gap:8px;padding:16px 20px;pointer-events:none;
               background:var(--bg-page);border:1px solid var(--border);border-radius:var(--r-pop);box-shadow:var(--shadow-pop);
               font-size:.875rem;color:var(--text-2)}
#map-empty .ei{font-size:2rem;line-height:1}
#map-empty .ea{display:flex;gap:8px}
#map-empty .ea button{pointer-events:auto}
#drawhint{font-size:.75rem;color:var(--text-2);padding:6px}
.leaflet-container{font:inherit;background:var(--bg-sidebar)}
.leaflet-bar{border:1px solid var(--border);box-shadow:none;border-radius:var(--r-md);overflow:hidden}
.leaflet-bar a{width:28px;height:28px;line-height:28px;background:var(--bg-elevated);color:var(--text);border-bottom:1px solid var(--border)}
.leaflet-bar a:hover{background:var(--bg-hover)}
.leaflet-control-attribution{font-size:10px;background:color-mix(in srgb,var(--bg-page) 75%,transparent);color:var(--text-2)}

/* 蓝点（divIcon） */
.tp-dot{position:relative;width:20px;height:20px;pointer-events:none}
.tp-dot i{position:absolute;inset:5px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 2px var(--bg-page)}
.tp-dot s{position:absolute;inset:0;border-radius:50%;background:var(--accent);opacity:0}
.tp-dot.live s{animation:pulse 1.6s var(--ease) infinite}
.tp-dot.paused i{background:var(--warning)}
.tp-dot.paused s{background:var(--warning);opacity:.28}
.tp-dot.holding s{opacity:.28}
.tp-dot.idle i{background:var(--chip-idle-dot)}
@keyframes pulse{0%{transform:scale(.6);opacity:.45}100%{transform:scale(1.7);opacity:0}}
#map-wrap.tiles-failed #map{background-image:linear-gradient(var(--border) 1px,transparent 1px),linear-gradient(90deg,var(--border) 1px,transparent 1px);background-size:24px 24px}

/* 进度 + 坐标 */
.progress-row{display:flex;align-items:center;gap:12px;margin-top:10px;font-size:.75rem;color:var(--text-2)}
#progress{flex:1;height:2px;background:var(--divider);border-radius:1px;overflow:hidden}
#progress-bar{height:100%;width:0;background:var(--accent);transition:width 200ms linear}
#coord{font-family:var(--font-mono);font-size:.75rem}

/* 日志 code block */
#log{max-height:280px;overflow-y:auto;background:var(--bg-sidebar);border:1px solid var(--border);border-radius:var(--r-block);
     padding:8px 10px;font-family:var(--font-mono);font-size:.75rem;line-height:1.6;white-space:pre-wrap;word-break:break-all}
#log .l-err{color:var(--danger)} #log .l-warn{color:var(--warning)} #log .l-coord{color:var(--text-2)}

/* sticky 操作条 + toast + tooltip */
.actions{position:sticky;bottom:0;display:flex;gap:8px;padding:12px 0;margin-top:16px;
         background:linear-gradient(to top,var(--bg-page) 70%,transparent)}
#toast-root{position:fixed;left:16px;bottom:16px;z-index:1000;display:flex;flex-direction:column;gap:8px;max-width:min(420px,90vw)}
.toast{display:flex;align-items:center;gap:10px;background:var(--ink);color:var(--on-ink);padding:10px 12px;
       border-radius:var(--r-block);font-size:.8125rem;box-shadow:var(--shadow-pop);animation:rise var(--t-mid) var(--ease)}
.toast .tm{flex:1;min-width:0}
.toast button{background:transparent;border-color:currentColor;color:inherit;height:24px;flex:none}
@keyframes rise{from{transform:translateY(8px);opacity:0}to{transform:none;opacity:1}}
.tip{position:fixed;z-index:1100;padding:4px 8px;background:var(--ink);color:var(--on-ink);font-size:.6875rem;
     border-radius:var(--r-sm);pointer-events:none;box-shadow:var(--shadow-pop);max-width:260px}

/* skeleton */
.sk{height:12px;border-radius:var(--r-sm);background:linear-gradient(90deg,var(--bg-hover) 25%,var(--bg-sidebar) 37%,var(--bg-hover) 63%);
    background-size:400% 100%;animation:shimmer 1.2s ease infinite}
@keyframes shimmer{0%{background-position:100% 0}100%{background-position:0 0}}
#dev-sk{display:none;flex-direction:column;gap:6px}
#dev-sk .sk:nth-child(2){width:85%}
#dev-sk .sk:nth-child(3){width:45%}
#device-card.loading > b,#device-card.loading > #devhint,#device-card.loading > .ud{display:none}
#device-card.loading #dev-sk{display:flex}

/* 上传中 / 预览中 / toast 退出 / callout 关闭 / 日志空态 */
.file-block.loading{pointer-events:none}
.file-block.loading::after{content:"";position:absolute;right:12px;top:0;bottom:0;margin:auto;width:14px;height:14px;
  border:2px solid var(--border-strong);border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
#map-loading{position:absolute;inset:0;z-index:6;display:flex;align-items:center;justify-content:center;gap:8px;
  background:color-mix(in srgb,var(--bg-page) 65%,transparent);font-size:.8125rem;color:var(--text-2)}
#map-loading .sp{width:16px;height:16px;flex:none;border:2px solid var(--border-strong);border-top-color:var(--accent);
  border-radius:50%;animation:spin .8s linear infinite}
.toast.out{opacity:0;transform:translateY(8px);transition:opacity var(--t-mid) var(--ease),transform var(--t-mid) var(--ease)}
.callout .cx{flex:none;width:24px;height:24px;padding:0}
#log:empty::before{content:"暂无日志。";color:var(--text-2)}
.lg-lb{font-size:.75rem;color:var(--text-2);cursor:pointer}

/* 窄屏抽屉 */
#menu-btn{display:none;position:fixed;top:12px;left:12px;z-index:800;background:var(--bg-elevated);box-shadow:var(--shadow-pop)}
#scrim{display:none;position:fixed;inset:0;z-index:850;background:rgba(15,15,15,.35)}
@media (max-width:1279px){:root{--sidebar-w:200px}#page{padding:24px 24px 96px}#map{height:clamp(320px,48vh,560px)}}
@media (max-width:1023px){
  #side{position:fixed;left:0;top:0;z-index:900;transform:translateX(-100%);transition:transform var(--t-mid) var(--ease);box-shadow:var(--shadow-pop)}
  #side.open{transform:none}
  #scrim.open{display:block}
  #page{padding:20px 20px 96px}
  .prop{grid-template-columns:1fr;gap:2px;padding:6px}
  #map{height:45vh}
  #menu-btn{display:inline-flex}
  .page-hd{padding-left:36px}
}
@media (max-width:767px){#page{padding:16px 16px 96px}#map{height:40vh}.actions{flex-wrap:wrap}.actions button{flex:1}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}html{scroll-behavior:auto}}
</style>
</head>
<body>
<div id="app">
  <aside id="side">
    <div class="side-brand"><span>📍</span><span>Track Player</span></div>
    <div class="side-sec">导航</div>
    <a class="side-item active" href="#sec-play" data-spy="sec-play">▶️ 播放台</a>
    <a class="side-item" href="#sec-map" data-spy="sec-map">🗺️ 轨迹地图</a>
    <a class="side-item" href="#sec-log" data-spy="sec-log">📜 运行日志</a>
    <div class="side-spacer"></div>
    <div class="device-card" id="device-card">
      <div id="dev-sk"><div class="sk"></div><div class="sk"></div><div class="sk"></div></div>
      <b id="dev-name">未检测到设备</b>
      <span id="devhint">连接 iPhone 后点「刷新设备」。</span>
      <div class="ud"><code id="dev-udid"></code>
        <button class="icon ghost" id="udid-copy" aria-label="复制 UDID" disabled>⧉</button></div>
    </div>
    <div class="side-sec" id="sec-look">外观</div>
    <button class="side-item" id="theme-btn">🌗 主题：浅色</button>
    <button class="side-item" id="font-btn">Aa 字体：默认</button>
  </aside>

  <main id="page">
    <header class="page-hd">
      <span class="page-icon">📍</span>
      <div>
        <h1 id="page-title">轨迹播放</h1>
        <div id="page-sub">在 iPhone 真机上沿 GPX / KML / GeoJSON 轨迹移动定位</div>
      </div>
      <span class="chip" id="chip" aria-live="polite"><span id="status">空闲</span></span>
    </header>

    <div id="callout-root"></div>

    <section class="block" id="sec-play">
      <div class="prop"><span class="k"><label for="device">设备</label></span>
        <span class="v"><select id="device"></select>
          <button class="icon" id="refresh" aria-label="刷新设备" title="刷新设备">↻</button></span></div>

      <div class="prop"><span class="k">轨迹</span>
        <span class="v">
          <label class="file-block" id="file-block">
            <span class="fi">📄</span>
            <span><span class="t" id="upload-name">上传轨迹文件</span>
            <span class="s" id="upload-meta">GPX / KML / GeoJSON · 或拖到此处</span></span>
            <input type="file" id="file" accept=".gpx,.kml,.geojson,.json" aria-label="上传轨迹文件">
          </label>
        </span></div>
      <div class="prop"><span class="k">摘要</span><span class="v" id="summary">尚未加载轨迹</span></div>

      <div class="prop"><span class="k"><label for="preset">速度</label></span>
        <span class="v"><select id="preset"></select>
          <span class="seg" role="group" aria-label="速度模式">
            <button type="button" id="seg-speed" aria-pressed="true">定速</button>
            <button type="button" id="seg-scale" aria-pressed="false">倍速</button>
          </span>
          <input type="number" id="speed" step="0.1" min="0.1" aria-label="定速（米每秒）"><span class="unit" id="speed-unit">m/s</span>
          <input type="number" id="scale" step="0.1" min="0.1" aria-label="倍速" hidden><span class="unit" id="scale-unit" hidden>×</span>
        </span></div>

      <div class="prop"><span class="k"><label for="repeat">循环</label></span>
        <span class="v"><input type="number" id="repeat" min="1" step="1" value="1"><span class="unit">遍</span></span></div>

      <div class="prop"><span class="k">坐标系</span>
        <span class="v"><input type="checkbox" class="switch" id="gcj" role="switch">
          <label for="gcj" data-tip="高德/腾讯导出的轨迹需勾选，否则会双重偏移约 500 m。">源是 GCJ-02（高德/腾讯）</label></span></div>
    </section>

    <section class="block" id="sec-map">
      <div class="block-hd"><span>🗺️ 地图 · 高德 GCJ-02</span><span class="sp"></span></div>
      <div id="map-wrap">
        <div id="map" aria-label="轨迹地图"></div>
        <div id="map-toolbar" class="tp-pop">
          <button class="icon" id="draw" aria-label="画轨迹" title="画轨迹">✏️</button>
          <button class="icon" id="undo" aria-label="撤销点" title="撤销点" disabled>↩</button>
          <button class="icon" id="dclear" aria-label="清空绘制" title="清空绘制" disabled>🗑</button>
          <button id="duse" disabled>用绘制轨迹</button>
        </div>
        <div id="draw-badge" hidden></div>
        <div id="map-empty">
          <div class="ec">
            <div class="ei">🗺️</div>
            <div>还没有轨迹</div>
            <div class="ea">
              <button id="empty-upload">上传文件</button>
              <button id="empty-draw">在地图上画一条</button>
            </div>
          </div>
        </div>
        <div id="map-loading" hidden><span class="sp"></span>正在生成轨迹…</div>
      </div>
      <div id="drawhint">在地图上点选路径点，至少 2 点。</div>
      <div class="progress-row">
        <div id="progress"><div id="progress-bar"></div></div>
        <span id="progress-label">0 / 0</span>
        <span id="coord">—</span>
      </div>
    </section>

    <section class="block">
      <button class="toggle-hd" id="adv-toggle" aria-expanded="false" aria-controls="adv-bd">
        <span class="caret">▸</span> 高级参数
      </button>
      <div class="toggle-bd" id="adv-bd" hidden>
        <div class="prop"><span class="k"><label for="duration">时长</label></span>
          <span class="v"><input type="number" id="duration" min="1" step="1" placeholder="空 = 手动停止">
          <span class="unit">秒</span></span></div>
        <div class="prop"><span class="k">地图跟随</span>
          <span class="v"><input type="checkbox" class="switch" id="follow" role="switch" checked>
          <label for="follow">蓝点出视野时自动平移</label></span></div>
        <div class="prop"><span class="k">暗色地图</span>
          <span class="v"><input type="checkbox" class="switch" id="map-dim" role="switch">
          <label for="map-dim">暗色主题下保留亮色瓦片（标注更清晰）</label></span></div>
      </div>
    </section>

    <section class="block" id="sec-log">
      <button class="toggle-hd" id="log-toggle" aria-expanded="false" aria-controls="log-bd">
        <span class="caret">▸</span> 运行日志
      </button>
      <div class="toggle-bd" id="log-bd" hidden>
        <div class="block-hd"><span class="sp"></span>
          <button class="icon ghost" id="log-copy" aria-label="复制日志" title="复制日志">⧉</button>
          <button class="icon ghost" id="log-clear" aria-label="清空日志" title="清空日志">🗑</button>
          <input type="checkbox" class="switch" id="log-scroll" role="switch" checked aria-label="自动滚动">
          <label class="lg-lb" for="log-scroll">自动滚动</label>
        </div>
        <div id="log" role="log" aria-live="off" aria-label="运行日志"></div>
      </div>
    </section>

    <div class="actions">
      <button class="primary" id="play" aria-keyshortcuts="Alt+P">▶ 播放</button>
      <button id="pause" aria-keyshortcuts="Alt+P" disabled>⏸ 暂停</button>
      <button id="stop" aria-keyshortcuts="Alt+S" disabled>⏹ 停止</button>
      <button class="danger" id="clear" aria-keyshortcuts="Alt+R">📍 恢复定位</button>
    </div>
    <div class="sr-only" id="sr-status" aria-live="polite"></div>
  </main>
</div>
<div id="toast-root"></div>
<div id="scrim"></div>
<button class="icon" id="menu-btn" aria-label="打开侧栏">☰</button>
<script>
const $ = id => document.getElementById(id);
const token = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const map = L.map('map', { zoomControl:false }).setView([39.9042, 116.4074], 12);
L.control.zoom({ position:'bottomright' }).addTo(map);
const TILE_URL = 'https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}';
const tiles = L.tileLayer(TILE_URL, { subdomains:'1234', maxZoom:18, attribution:'© 高德地图' }).addTo(map);
let tileErrors = 0, tilesFailed = false, tileCallout = null;
function checkTiles(){
  const imgs = [...document.querySelectorAll('img.leaflet-tile')];
  const broken = imgs.filter(i => i.complete && i.naturalWidth === 0).length;
  const failed = tileErrors >= 4 || (imgs.length >= 4 && broken * 2 > imgs.length);
  if (failed === tilesFailed) return;
  tilesFailed = failed;
  $('map-wrap').classList.toggle('tiles-failed', failed);
  if (failed) tileCallout = callout('底图加载失败（离线或代理）。轨迹线与播放不受影响。', 'gray');
  else if (tileCallout){ tileCallout.remove(); tileCallout = null; }
}
tiles.on('tileerror', () => { tileErrors++; checkTiles(); });
tiles.on('load', () => { tileErrors = 0; checkTiles(); });
setInterval(checkTiles, 5000);
let trackLine = null, marker = null, startMarker = null, totalPoints = 0, seen = 0;
let curState = 'idle', hasTrack = false, restoring = false, detecting = false;
let drawing = false, drawnPts = [], drawnLine = null, drawnDots = [];
let mode = 'speed', cfgDefaults = { speed:3, scale:2 };

const LOG_MAX = 500; let logBuf = [], logRaf = 0;
function classify(line){
  if (/^error/i.test(line)) return 'l-err';
  if (/^warning/i.test(line)) return 'l-warn';
  if (/set location to/i.test(line)) return 'l-coord';
  return '';
}
function log(line){
  logBuf.push(line);
  if (logRaf) return;
  logRaf = requestAnimationFrame(() => {
    logRaf = 0;
    const el = $('log');
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    const frag = document.createDocumentFragment();
    for (const l of logBuf){
      const d = document.createElement('div'), c = classify(l);
      if (c) d.className = c;
      d.textContent = l; frag.appendChild(d);
    }
    logBuf = [];
    el.appendChild(frag);
    while (el.childElementCount > LOG_MAX) el.removeChild(el.firstElementChild);
    if ($('log-scroll').checked && nearBottom) el.scrollTop = el.scrollHeight;
  });
}
function fmtSize(n){ return n < 1024 ? n + ' B' : n < 1048576 ? (n/1024).toFixed(1) + ' KB' : (n/1048576).toFixed(1) + ' MB'; }

const ERR_ZH = {
  'only .gpx / .kml / .geojson / .json files are supported':'文件不支持：仅 .gpx / .kml / .geojson / .json。',
  'empty file':'文件为空。',
  'no track uploaded yet':'还没有轨迹，请先上传或绘制。',
  'already playing':'已有播放会话，请先停止。',
  'not playing':'当前没有播放会话。',
  'draw at least 2 points on the map first':'至少需要 2 个绘制点。',
  'stop playback before clearing':'播放中不能恢复定位，请先停止。'
};
function handleError(e){
  const raw = (e && e.message) || String(e);
  const msg = ERR_ZH[raw] || raw;
  toast(msg, 'err'); callout(msg, 'err');
}
function toast(msg, kind, action){
  const root = $('toast-root');
  while (root.childElementCount >= 3) root.removeChild(root.firstElementChild);
  const el = document.createElement('div');
  el.className = 'toast';
  el.setAttribute('role', kind === 'err' ? 'alert' : 'status');
  const tm = document.createElement('span'); tm.className = 'tm'; tm.textContent = msg; el.appendChild(tm);
  const timer = setTimeout(dismiss, 4000);
  function dismiss(){ clearTimeout(timer); el.classList.add('out'); setTimeout(() => el.remove(), 200); }
  if (action){
    const b = document.createElement('button'); b.textContent = action.label;
    b.onclick = () => { dismiss(); action.fn(); }; el.appendChild(b);
  }
  const x = document.createElement('button'); x.textContent = '✕'; x.setAttribute('aria-label', '关闭提示');
  x.onclick = dismiss; el.appendChild(x);
  root.appendChild(el);
}
const CALLOUT_ICON = { info:'💡', ok:'✅', warn:'⚠️', err:'⛔', gray:'ℹ️' };
function callout(msg, kind, cta){
  const root = $('callout-root');
  const transient = root.querySelectorAll('.callout:not(#ctx-callout)');
  if (transient.length >= 2) transient[0].remove();
  const k = kind || 'info';
  const el = document.createElement('div'); el.className = 'callout ' + k;
  const ci = document.createElement('span'); ci.className = 'ci'; ci.textContent = CALLOUT_ICON[k] || '💡';
  const cb = document.createElement('div'); cb.className = 'cb'; cb.textContent = msg;
  el.append(ci, cb);
  if (cta){
    const ca = document.createElement('div'); ca.className = 'ca';
    const b = document.createElement('button'); b.textContent = cta.label; b.onclick = cta.fn;
    ca.appendChild(b); cb.appendChild(ca);
  }
  const x = document.createElement('button'); x.className = 'icon ghost cx'; x.textContent = '✕';
  x.setAttribute('aria-label', '关闭'); x.onclick = () => el.remove(); el.appendChild(x);
  root.appendChild(el);
  return el;
}
function clearCallout(){ $('callout-root').querySelectorAll('.callout:not(#ctx-callout)').forEach(el => el.remove()); }
async function busy(btn, fn){
  const was = btn.disabled;
  btn.disabled = true; btn.setAttribute('aria-busy', 'true');
  try { return await fn(); }
  finally {
    btn.removeAttribute('aria-busy');
    // 状态按钮的 disabled 由 syncButtons 裁决，回填旧值会覆盖播放锁
    if (['play','pause','stop','clear','refresh'].includes(btn.id)) syncButtons();
    else btn.disabled = was;
  }
}
async function copyText(text){
  if (navigator.clipboard && window.isSecureContext){
    try { await navigator.clipboard.writeText(text); return; } catch(_){ /* 未聚焦/被拒时退回 execCommand */ }
  }
  const ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.focus(); ta.select();
  const ok = document.execCommand('copy'); ta.remove();
  if (!ok) throw new Error('copy failed');
}
let tipEl = null, tipTimer = 0;
function hideTip(){ clearTimeout(tipTimer); if (tipEl){ tipEl.remove(); tipEl = null; } }
function showTip(target){
  const text = target.getAttribute('data-tip');
  hideTip();
  if (!text) return;
  tipEl = document.createElement('div'); tipEl.className = 'tip'; tipEl.textContent = text;
  document.body.appendChild(tipEl);
  const r = target.getBoundingClientRect(), tw = tipEl.offsetWidth, th = tipEl.offsetHeight;
  tipEl.style.left = Math.min(Math.max(8, r.left + r.width / 2 - tw / 2), innerWidth - tw - 8) + 'px';
  tipEl.style.top = (r.top - th - 6 < 8 ? r.bottom + 6 : r.top - th - 6) + 'px';
}
function tipTarget(e){ return e.target instanceof Element ? e.target.closest('[data-tip]') : null; }
document.addEventListener('mouseover', e => { const t = tipTarget(e); if (!t) return; clearTimeout(tipTimer); tipTimer = setTimeout(() => showTip(t), 400); });
document.addEventListener('mouseout', e => { if (tipTarget(e)) hideTip(); });
document.addEventListener('focusin', e => { const t = tipTarget(e); if (t) showTip(t); });
document.addEventListener('focusout', hideTip);
window.addEventListener('scroll', hideTip, true);

const CHIP_TEXT = { idle:'空闲 · 真实定位已恢复', playing:'播放中', paused:'已暂停 · 坐标冻结并保持',
                    holding:'已完成 · 持有末点', error:'连接中断',
                    detecting:'正在检测设备…', restoring:'正在恢复真实定位…' };
const CHIP_PLAIN = ['idle', 'detecting', 'restoring'];
function chipState(){
  if (restoring) return 'restoring';
  if (detecting && curState === 'idle') return 'detecting';
  return (curState === 'playing' && totalPoints > 0 && seen >= totalPoints) ? 'holding' : curState;
}
function renderChip(){
  const s = chipState(), text = CHIP_TEXT[s] || s;
  $('chip').className = 'chip' + (CHIP_PLAIN.includes(s) ? '' : ' ' + s);
  $('status').textContent = text;
  $('sr-status').textContent = text;
}
function renderProgress(){
  $('progress-bar').style.width = (totalPoints ? Math.min(100, seen / totalPoints * 100) : 0) + '%';
  $('progress-label').textContent = `${seen} / ${totalPoints || 0}`;
}
function syncButtons(){
  const active = curState === 'playing' || curState === 'paused';
  const dev = !!$('device').value, lock = restoring || detecting || curState === 'error';
  $('play').disabled = active || lock || !hasTrack || !dev;
  $('play').setAttribute('data-tip', !dev ? '请先连接 iPhone。' : !hasTrack ? '请先上传或绘制轨迹。' : active ? '已有播放会话。' : '开始播放（Alt+P）。');
  $('pause').disabled = !active || lock;
  $('pause').setAttribute('data-tip', active ? (curState === 'paused' ? '继续播放（Alt+P）。' : '暂停播放（Alt+P）。') : '播放中才能暂停。');
  $('stop').disabled = !active || lock;
  $('stop').setAttribute('data-tip', active ? '停止并恢复真实定位（Alt+S）。' : '没有正在进行的播放。');
  $('clear').disabled = active || lock;
  $('clear').setAttribute('data-tip', active ? '播放中不能恢复定位，请先停止。' : '立即恢复真实定位（Alt+R）。');
  $('refresh').disabled = detecting;
}
function setStatus(s){
  curState = s;
  $('pause').textContent = s === 'paused' ? '▶ 继续' : '⏸ 暂停';
  // 恢复真实定位后读不到手机坐标，留着旧点会误导，直接撤掉
  if (s === 'idle' && marker){ map.removeLayer(marker); marker = null; dotState = ''; }
  syncDot();
  syncButtons();
  renderChip();
}
function renderContext(){
  const old = $('ctx-callout'); if (old) old.remove();
  if (!detecting){
    const root = $('callout-root');
    if (!$('device').value){
      const el = document.createElement('div');
      el.className = 'callout err'; el.id = 'ctx-callout';
      el.innerHTML = '<span class="ci">⛔</span><div class="cb"><b>未检测到 iPhone。</b>' +
        '<ol><li>安装 iTunes，确认 Apple Mobile Device Support（usbmux）服务在运行。</li>' +
        '<li>开启开发者模式：设置 → 隐私与安全性 → 开发者模式，打开后重启确认。</li>' +
        '<li>USB 连接、解锁手机，弹窗点「信任此电脑」。</li></ol>' +
        '<div class="ca"><button id="ctx-retry">重新检测</button></div></div>';
      root.prepend(el);
      $('ctx-retry').onclick = () => refreshDevices();
    } else if (!hasTrack){
      const el = callout('设备已连接。上传轨迹文件，或直接在地图上画一条路线。', 'info');
      el.id = 'ctx-callout';
      root.prepend(el);
    }
  }
  syncButtons();
}
function params(){
  const p = { repeat: $('repeat').value || 1, from_gcj02: $('gcj').checked ? 1 : 0 };
  const preset = $('preset').value;
  if (preset && preset !== '__custom') p.preset = preset;
  else if ($('speed').value) p.speed = $('speed').value;
  else if ($('scale').value) p.scale = $('scale').value;
  if ($('duration').value) p.duration = $('duration').value;
  if ($('device').value) p.udid = $('device').value;
  return p;
}
async function api(path, opts){
  const r = await fetch(path, opts);
  const text = await r.text();
  let j;
  try { j = JSON.parse(text); } catch(_){ throw new Error(`服务响应异常（HTTP ${r.status}）。`); }
  if (j.error) throw new Error(j.error);
  return j;
}

function showDevice(){
  const sel = $('device'), o = sel.options[sel.selectedIndex];
  $('dev-name').textContent = o && sel.value ? o.textContent : '未检测到设备';
  $('dev-udid').textContent = sel.value || '';
  $('udid-copy').disabled = !sel.value;
  $('device-card').classList.toggle('online', !!sel.value);
  $('devhint').textContent = sel.value ? '' : '连接 iPhone 后点「刷新设备」。';
}
async function refreshDevices(){
  detecting = true;
  $('device-card').classList.add('loading');
  renderChip(); syncButtons();
  try {
    const j = await api('/api/devices');
    const sel = $('device'), prev = sel.value;
    sel.innerHTML = '';
    if (!j.devices.length) sel.innerHTML = '<option value="">未检测到 iPhone</option>';
    else {
      for (const d of j.devices){ const o=document.createElement('option'); o.value=d.udid; o.textContent=`${d.name} (iOS ${d.ios}, ${d.connection})`; sel.appendChild(o); }
      if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
    }
    showDevice();
    if (j.last_point && !marker){
      const [glat, glon] = j.last_point;
      map.setView([glat, glon], 16);
    }
  } catch(e){ handleError(e); }
  finally {
    detecting = false;
    $('device-card').classList.remove('loading');
    renderChip(); renderContext();
  }
}
async function loadConfig(){
  try {
    const c = await api('/api/config');
    const sel = $('preset'); sel.innerHTML = '';
    for (const p of c.presets){ const o=document.createElement('option'); o.value=p.label; o.textContent=`${p.label} (${p.kmh} km/h)`; sel.appendChild(o); }
    const o=document.createElement('option'); o.value='__custom'; o.textContent='自定义…'; sel.appendChild(o);
    cfgDefaults = { speed:c.default_speed_mps, scale:c.default_scale };
    $('speed').value = c.default_speed_mps; $('scale').value = c.default_scale; $('repeat').value = c.default_repeat;
    syncPreset();
  } catch(e){ handleError(e); }
}
function syncPreset(){
  const sel = $('preset');
  const custom = sel.value === '__custom' || !sel.options.length;
  ['seg-speed','seg-scale','speed','scale'].forEach(id => $(id).disabled = !custom);
  if (!custom){ $('speed').value = ''; $('scale').value = ''; return; }
  const f = mode === 'speed' ? 'speed' : 'scale';
  if (!$(f).value) $(f).value = cfgDefaults[f];
}
function setMode(m){
  mode = m;
  const sp = m === 'speed';
  $('seg-speed').setAttribute('aria-pressed', String(sp));
  $('seg-scale').setAttribute('aria-pressed', String(!sp));
  $('speed').hidden = !sp; $('speed-unit').hidden = !sp;
  $('scale').hidden = sp; $('scale-unit').hidden = sp;
  if (sp) $('scale').value = ''; else $('speed').value = '';
  syncPreset();
  preview();
}
function restyleLayers(){
  if (trackLine) trackLine.setStyle({ color:token('--accent'), weight:4, opacity:.9 });
  if (startMarker) startMarker.setStyle({ color:token('--bg-page'), fillColor:token('--success') });
  if (drawnLine) drawnLine.setStyle({ color:token('--draw') });
  drawnDots.forEach(d => d.setStyle({ color:token('--bg-page'), fillColor:token('--draw') }));
}
function updateEmpty(){ $('map-empty').hidden = hasTrack || drawing; }
function resetTrack(){
  hasTrack = false; totalPoints = 0; seen = 0;
  if (trackLine){ map.removeLayer(trackLine); trackLine = null; }
  if (startMarker){ map.removeLayer(startMarker); startMarker = null; }
  $('summary').textContent = '尚未加载轨迹';
  $('upload-name').textContent = '上传轨迹文件';
  $('upload-meta').textContent = 'GPX / KML / GeoJSON · 或拖到此处';
  $('coord').textContent = '—';
  renderProgress(); updateEmpty(); renderContext();
}
function drawTrack(points){
  if (trackLine) map.removeLayer(trackLine);
  if (startMarker) map.removeLayer(startMarker);
  trackLine = L.polyline(points, { color:token('--accent'), weight:4, opacity:.9 }).addTo(map);
  startMarker = L.circleMarker(points[0], { radius:6, color:token('--bg-page'), weight:2, fillColor:token('--success'), fillOpacity:1 }).addTo(map);
  map.fitBounds(trackLine.getBounds(), { padding:[40,40], maxZoom:17 });
}
let previewSeq = 0;
async function preview(){
  if (!hasTrack) return;
  const seq = ++previewSeq;
  const q = new URLSearchParams(params());
  $('map-loading').hidden = false;
  try {
    const j = await api('/api/preview?' + q.toString());
    if (seq !== previewSeq) return;
    totalPoints = j.summary.n;
    $('summary').textContent = `${j.name} · ${j.summary.n} 点 · ${j.summary.km} km · ${j.summary.dur_s} s`;
    drawTrack(j.points);
    renderProgress(); renderChip();
  } catch(e){
    if (seq !== previewSeq) return;
    if (e.message === 'no track uploaded yet') resetTrack();
    handleError(e);
  }
  finally { if (seq === previewSeq) $('map-loading').hidden = true; }
}
async function upload(){
  const f = $('file').files[0]; if (!f) return;
  const block = $('file-block');
  block.classList.add('loading');
  $('upload-meta').textContent = `正在上传 ${f.name}…`;
  try {
    const buf = await f.arrayBuffer();
    await api('/api/track?name=' + encodeURIComponent(f.name), { method:'POST', body:buf });
    hasTrack = true;
    $('upload-name').textContent = f.name;
    $('upload-meta').textContent = `${fmtSize(buf.byteLength)} · 点击可替换`;
    clearCallout(); updateEmpty(); renderContext();
    await preview();
  } catch(e){
    $('upload-meta').textContent = '上传失败，点击重试。';
    handleError(e);
  } finally {
    block.classList.remove('loading');
    $('file').value = '';
  }
}
let dotState = '';
function dotClass(){
  const s = chipState();
  return s === 'playing' ? 'live' : s === 'paused' ? 'paused' : s === 'holding' ? 'holding' : 'idle';
}
function dotIcon(cls){
  return L.divIcon({ className:'', html:'<div class="tp-dot ' + cls + '"><s></s><i></i></div>', iconSize:[20,20], iconAnchor:[10,10] });
}
function syncDot(){
  if (marker){
    const c = dotClass();
    if (c !== dotState){ marker.setIcon(dotIcon(c)); dotState = c; }
  }
}
function ensureMarker(lat, lon){
  if (!marker){
    dotState = dotClass();
    marker = L.marker([lat,lon], { icon:dotIcon(dotState), interactive:false, keyboard:false, zIndexOffset:1000 }).addTo(map);
  } else { marker.setLatLng([lat,lon]); }
  syncDot();
  if ($('follow').checked && !map.getBounds().contains([lat,lon])) map.panTo([lat,lon], { animate:true, duration:.25 });
}

function updateDrawBadge(){
  $('draw-badge').textContent = `绘制中 · ${drawnPts.length} 点 · ` +
    (drawnPts.length < 2 ? '至少 2 点才能生成 · Esc 退出' : '点「用绘制轨迹」生成 · Esc 退出');
}
function redrawDrawn(){
  if (drawnLine){ map.removeLayer(drawnLine); drawnLine = null; }
  drawnDots.forEach(d => map.removeLayer(d)); drawnDots = [];
  if (drawnPts.length >= 2) drawnLine = L.polyline(drawnPts, { color:token('--draw'), weight:3, dashArray:'6 6' }).addTo(map);
  for (const p of drawnPts) drawnDots.push(L.circleMarker(p, { radius:4, color:token('--bg-page'), weight:2, fillColor:token('--draw'), fillOpacity:1 }).addTo(map));
  $('undo').disabled = !drawnPts.length; $('dclear').disabled = !drawnPts.length; $('duse').disabled = drawnPts.length < 2;
  if (drawing) updateDrawBadge();
}
function setDrawing(on){
  drawing = on;
  $('draw').classList.toggle('primary', on);
  $('draw').setAttribute('aria-label', on ? '结束绘制' : '画轨迹');
  $('draw').title = on ? '结束绘制（Esc）' : '画轨迹';
  $('map-wrap').classList.toggle('drawing', on);
  $('draw-badge').hidden = !on;
  $('drawhint').textContent = on ? '绘制中：点击地图添加点，Esc 退出，Ctrl+Z 撤销。' : '在地图上点选路径点，至少 2 点。';
  $('map').setAttribute('aria-label', on ? '轨迹地图 · 绘制中：点击添加路径点，Esc 退出' : '轨迹地图');
  if (on) updateDrawBadge();
  updateEmpty();
}
map.on('click', e => { if (!drawing) return; drawnPts.push([e.latlng.lat, e.latlng.lng]); redrawDrawn(); });
function undoPoint(){ drawnPts.pop(); redrawDrawn(); }
$('draw').onclick = () => setDrawing(!drawing);
$('undo').onclick = undoPoint;
$('dclear').onclick = () => { drawnPts = []; redrawDrawn(); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape'){ if (drawing) setDrawing(false); else closeDrawer(); return; }
  const typing = e.target instanceof Element && e.target.closest('input,textarea,select');
  if ((e.ctrlKey || e.metaKey) && !e.altKey && !typing && e.key.toLowerCase() === 'z' && drawing){ e.preventDefault(); undoPoint(); }
  if (e.altKey && !e.ctrlKey && !e.metaKey){
    const k = e.key.toLowerCase();
    if (k === 'p'){ e.preventDefault(); (curState === 'playing' || curState === 'paused' ? $('pause') : $('play')).click(); }
    else if (k === 's'){ e.preventDefault(); $('stop').click(); }
    else if (k === 'r'){ e.preventDefault(); $('clear').click(); }
  }
});
const post = (path, obj) => api(path, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(obj) });
async function restore(btn, fn){
  restoring = true; syncButtons(); renderChip();
  try { await busy(btn, fn); setStatus('idle'); }
  catch(e){ handleError(e); }
  finally { restoring = false; syncButtons(); renderChip(); }
}
$('duse').onclick = () => busy($('duse'), async () => {
  try {
    await post('/api/drawn', { points: drawnPts });
    hasTrack = true; setDrawing(false); drawnPts = []; redrawDrawn();
    $('upload-name').textContent = 'drawn track.gpx';
    $('upload-meta').textContent = '地图手绘 · 点击可替换';
    clearCallout(); updateEmpty(); renderContext();
    await preview();
  } catch(e){ handleError(e); }
});
$('pause').onclick = () => busy($('pause'), async () => {
  try { await post('/api/pause', { paused: curState !== 'paused' }); } catch(e){ handleError(e); }
});

$('refresh').onclick = () => busy($('refresh'), refreshDevices);
$('device').onchange = () => { showDevice(); renderContext(); };
$('file').onchange = upload;
$('preset').onchange = () => { syncPreset(); preview(); };
['speed','scale','repeat','gcj'].forEach(id => $(id).onchange = preview);
$('seg-speed').onclick = () => setMode('speed');
$('seg-scale').onclick = () => setMode('scale');
$('play').onclick = () => busy($('play'), async () => {
  try {
    await post('/api/play', params());
    seen = 0; setStatus('playing');
    await preview();
    renderProgress();
  } catch(e){ handleError(e); }
});
$('stop').onclick = () => restore($('stop'), () => api('/api/stop', { method:'POST' }));
$('clear').onclick = () => restore($('clear'), () => post('/api/clear', { udid: $('device').value }));
$('empty-upload').onclick = () => $('file').click();
$('empty-draw').onclick = () => { closeDrawer(); setDrawing(true); };
$('udid-copy').onclick = async () => {
  const v = $('device').value; if (!v) return;
  try { await copyText(v); toast('UDID 已复制。'); } catch(e){ toast('复制失败，请手动选择复制。', 'err'); }
};
$('log-copy').onclick = async () => {
  const text = [...$('log').children].map(d => d.textContent).join('\n');
  if (!text){ toast('日志为空。'); return; }
  try { await copyText(text); toast('日志已复制。'); } catch(e){ toast('复制失败，请在日志区手动选择复制。', 'err'); }
};
$('log-clear').onclick = () => { $('log').textContent = ''; logBuf = []; toast('日志已清空。'); };
$('log-scroll').onchange = () => { if ($('log-scroll').checked){ const el = $('log'); el.scrollTop = el.scrollHeight; } };

const fb = $('file-block');
['dragenter','dragover'].forEach(ev => fb.addEventListener(ev, e => { e.preventDefault(); fb.classList.add('drag'); }));
['dragleave','drop'].forEach(ev => fb.addEventListener(ev, e => { e.preventDefault(); fb.classList.remove('drag'); }));
fb.addEventListener('drop', e => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  const dt = new DataTransfer(); dt.items.add(f); $('file').files = dt.files; upload();
});
['dragover','drop'].forEach(ev => window.addEventListener(ev, e => e.preventDefault()));

function bindToggle(btnId, bdId){
  const b = $(btnId), d = $(bdId);
  let timer = 0;
  b.onclick = () => {
    const open = b.getAttribute('aria-expanded') === 'true';
    b.setAttribute('aria-expanded', String(!open));
    clearTimeout(timer);
    if (open){
      d.style.height = d.scrollHeight + 'px';
      void d.offsetHeight;  // 先落一次布局：auto→0 不可插值，缺这步高度会瞬跳
      requestAnimationFrame(() => { d.style.height = '0px'; d.style.opacity = '0'; });
      timer = setTimeout(() => { d.hidden = true; d.style.height = ''; d.style.opacity = ''; }, 200);
    } else {
      d.hidden = false;
      d.style.height = '0px'; d.style.opacity = '0';
      requestAnimationFrame(() => { d.style.height = d.scrollHeight + 'px'; d.style.opacity = '1'; });
      timer = setTimeout(() => {
        d.style.height = ''; d.style.opacity = '';
        if (bdId === 'adv-bd') map.invalidateSize();
      }, 200);
      if (bdId === 'log-bd'){ const el = $('log'); el.scrollTop = el.scrollHeight; }
    }
  };
}
function syncDrawer(){
  const side = $('side');
  if (matchMedia('(max-width:1023px)').matches && !side.classList.contains('open')) side.setAttribute('inert','');
  else side.removeAttribute('inert');
}
function closeDrawer(focusMenu = true){
  const wasOpen = $('side').classList.contains('open');
  $('side').classList.remove('open'); $('scrim').classList.remove('open');
  syncDrawer();
  if (wasOpen && focusMenu && matchMedia('(max-width:1023px)').matches) $('menu-btn').focus();
}
$('menu-btn').onclick = () => {
  $('side').classList.add('open'); $('scrim').classList.add('open');
  syncDrawer();
  const first = $('side').querySelector('.side-item'); if (first) first.focus();
};
$('scrim').onclick = () => closeDrawer();

const spyLinks = Array.from(document.querySelectorAll('[data-spy]'));
const spySecs = spyLinks.map(a => $(a.dataset.spy)).filter(el => el && el.closest('#page'));
function setActiveSpy(id){ spyLinks.forEach(a => a.classList.toggle('active', a.dataset.spy === id)); }
let spyRaf = 0, spyLock = 0;
function updateSpy(){
  spyRaf = 0;
  if (!spySecs.length || performance.now() < spyLock) return;
  const doc = document.documentElement;
  const atBottom = doc.scrollHeight > window.innerHeight + 2 && window.innerHeight + window.scrollY >= doc.scrollHeight - 2;
  let cur = spySecs[0];
  if (atBottom) cur = spySecs[spySecs.length - 1];
  else for (const el of spySecs){ if (el.getBoundingClientRect().top <= 120) cur = el; }
  setActiveSpy(cur.id);
}
function queueSpy(){ if (!spyRaf) spyRaf = requestAnimationFrame(updateSpy); }
window.addEventListener('scroll', queueSpy, {passive:true});
window.addEventListener('resize', () => { queueSpy(); syncDrawer(); });
spyLinks.forEach(a => a.addEventListener('click', () => {
  setActiveSpy(a.dataset.spy);
  spyLock = performance.now() + 700;
  if (matchMedia('(max-width:1023px)').matches) closeDrawer(false);
}));
updateSpy();
syncDrawer();

const THEME_LABEL = { light:'浅色', dark:'深色' }, FONT_LABEL = { default:'默认', serif:'衬线', mono:'等宽' };
function applyTheme(t){
  document.documentElement.dataset.theme = t;
  localStorage.setItem('tp-theme', t);
  $('theme-btn').textContent = `🌗 主题：${THEME_LABEL[t] || t}`;
  restyleLayers();
}
function applyFont(f){
  document.documentElement.dataset.font = f;
  localStorage.setItem('tp-font', f);
  $('font-btn').textContent = `Aa 字体：${FONT_LABEL[f] || f}`;
}
$('theme-btn').onclick = () => applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
$('font-btn').onclick = () => { const order = ['default','serif','mono']; const cur = document.documentElement.dataset.font || 'default';
  applyFont(order[(order.indexOf(cur) + 1) % order.length]); };
$('map-dim').onchange = () => {
  const on = $('map-dim').checked;
  if (on) document.documentElement.dataset.mapDim = '1'; else delete document.documentElement.dataset.mapDim;
  localStorage.setItem('tp-map-dim', on ? '1' : '0');
};

const es = new EventSource('/api/stream');
es.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.type === 'log') log(m.line);
  else if (m.type === 'state') setStatus(m.state);
  else if (m.type === 'point'){
    seen++; ensureMarker(m.lat, m.lon); renderProgress();
    $('coord').textContent = `${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}`;
    renderChip();
  }
};

let sseCallout = null;
es.onerror = () => {
  curState = 'error'; syncButtons(); renderChip();
  if (!sseCallout) sseCallout = callout('与本地服务失联。请确认 trackplayer web 仍在运行；恢复后会自动重连。', 'err',
    { label:'刷新页面', fn:() => location.reload() });
};
es.onopen = () => {
  if (curState === 'error'){
    curState = 'idle'; syncButtons(); renderChip();
    refreshDevices();
    if (hasTrack) preview();
  }
  if (sseCallout){ sseCallout.remove(); sseCallout = null; }
};

bindToggle('adv-toggle','adv-bd');
bindToggle('log-toggle','log-bd');
applyTheme(document.documentElement.dataset.theme || 'light');
applyFont(document.documentElement.dataset.font || 'default');
$('map-dim').checked = localStorage.getItem('tp-map-dim') === '1';
setMode('speed');
updateEmpty();
renderChip();
syncButtons();
refreshDevices(); loadConfig();
</script>
</body>
</html>
"""

if __name__ == '__main__':
    main()