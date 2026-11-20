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
            return self.rfile.read(n) if n else b''

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
                    self._json({'devices': _devices()})
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
                self._json({'error': str(e)}, 400)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
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
<style>
  :root { --bg:#f5f6f8; --panel:#fff; --line:#e3e6ea; --accent:#2563eb; --text:#1f2937; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:system-ui,"Segoe UI","Microsoft YaHei",sans-serif; color:var(--text); background:var(--bg); }
  #app { display:flex; height:100vh; }
  #side { width:340px; min-width:340px; overflow-y:auto; padding:16px; background:var(--panel); border-right:1px solid var(--line); }
  #map { flex:1; }
  h1 { font-size:18px; margin:0 0 12px; }
  h2 { font-size:13px; margin:18px 0 8px; color:#6b7280; text-transform:uppercase; letter-spacing:.04em; }
  label { display:block; font-size:13px; margin:8px 0 3px; }
  select, input[type=text], input[type=number] { width:100%; padding:6px 8px; border:1px solid var(--line); border-radius:6px; font-size:13px; }
  .row { display:flex; gap:8px; }
  .row > * { flex:1; }
  button { padding:7px 12px; border:1px solid var(--line); border-radius:6px; background:#fff; cursor:pointer; font-size:13px; }
  button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
  button:disabled { opacity:.5; cursor:default; }
  .btns { display:flex; gap:8px; margin-top:12px; }
  .btns button { flex:1; }
  .hint { font-size:12px; color:#6b7280; margin-top:4px; }
  .status { font-size:13px; font-weight:600; margin-top:10px; }
  .status.playing { color:#16a34a; }
  .status.paused { color:#d97706; }
  #summary { font-size:12px; color:#374151; margin-top:6px; }
  #log { margin-top:8px; height:180px; overflow-y:auto; background:#0f172a; color:#cbd5e1; font-family:Consolas,monospace; font-size:11px; padding:8px; border-radius:6px; white-space:pre-wrap; }
  .check { display:flex; align-items:center; gap:6px; font-size:13px; margin-top:8px; }
  .check input { width:auto; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; background:var(--accent); border:2px solid #fff; box-shadow:0 0 0 1px var(--accent); }
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <h1>Track Player <span class="dot"></span></h1>

    <h2>设备</h2>
    <div class="row">
      <select id="device"></select>
      <button id="refresh" style="flex:0 0 auto">刷新</button>
    </div>
    <div class="hint" id="devhint"></div>

    <h2>轨迹</h2>
    <input type="file" id="file" accept=".gpx,.kml,.geojson,.json">
    <div class="row" style="margin-top:8px">
      <button id="draw">画轨迹</button>
      <button id="undo" disabled>撤销点</button>
      <button id="dclear" disabled>清空</button>
      <button id="duse" disabled>用绘制轨迹</button>
    </div>
    <div class="hint" id="drawhint">点「画轨迹」后在地图上逐点点击生成路线（GCJ-02），再「用绘制轨迹」。</div>
    <div id="summary"></div>

    <h2>参数</h2>
    <label>速度预设</label>
    <select id="preset"></select>
    <div class="row">
      <div><label>定速 (m/s)</label><input type="number" id="speed" step="0.1" min="0.1"></div>
      <div><label>倍速 (×)</label><input type="number" id="scale" step="0.1" min="0.1"></div>
    </div>
    <div class="row">
      <div><label>循环次数</label><input type="number" id="repeat" min="1" value="1"></div>
      <div><label>时长 (秒, 空=手动)</label><input type="number" id="duration" min="1"></div>
    </div>
    <div class="check"><input type="checkbox" id="gcj"><label for="gcj" style="margin:0">源是 GCJ-02（高德/腾讯）</label></div>
    <div class="check"><input type="checkbox" id="follow" checked><label for="follow" style="margin:0">地图跟随蓝点</label></div>

    <div class="btns">
      <button id="play" class="primary">播放</button>
      <button id="pause" disabled>暂停</button>
      <button id="stop" disabled>停止</button>
      <button id="clear">恢复定位</button>
    </div>
    <div class="status" id="status">空闲</div>

    <h2>日志</h2>
    <div id="log"></div>
  </div>
  <div id="map"></div>
</div>
<script>
const $ = id => document.getElementById(id);
const map = L.map('map').setView([39.9042, 116.4074], 12);
L.tileLayer('https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}',
  { subdomains:'1234', maxZoom:18, attribution:'© 高德地图' }).addTo(map);
let trackLine = null, marker = null, startMarker = null, totalPoints = 0, seen = 0;
let curState = 'idle', hasTrack = false;
let drawing = false, drawnPts = [], drawnLine = null, drawnDots = [];

function log(line){ const el=$('log'); el.textContent += line + '\n'; el.scrollTop = el.scrollHeight; }
function setStatus(s){
  curState = s;
  const active = s === 'playing' || s === 'paused';
  $('status').textContent = s === 'playing' ? '播放中' : s === 'paused' ? '已暂停（坐标冻结）' : '空闲';
  $('status').className = 'status' + (s === 'playing' ? ' playing' : s === 'paused' ? ' paused' : '');
  $('play').disabled = active; $('stop').disabled = !active; $('pause').disabled = !active;
  $('pause').textContent = s === 'paused' ? '继续' : '暂停';
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
async function api(path, opts){ const r = await fetch(path, opts); const j = await r.json(); if (j.error) throw new Error(j.error); return j; }

async function refreshDevices(){
  try {
    const j = await api('/api/devices');
    const sel = $('device'); sel.innerHTML = '';
    if (!j.devices.length){ sel.innerHTML = '<option value="">未检测到 iPhone</option>'; $('devhint').textContent = '装 iTunes → 开发者模式 → USB 连接并信任'; return; }
    $('devhint').textContent = '';
    for (const d of j.devices){ const o=document.createElement('option'); o.value=d.udid; o.textContent=`${d.name} (iOS ${d.ios}, ${d.connection})`; sel.appendChild(o); }
  } catch(e){ $('devhint').textContent = e.message; }
}
async function loadConfig(){
  const c = await api('/api/config');
  const sel = $('preset'); sel.innerHTML = '';
  for (const p of c.presets){ const o=document.createElement('option'); o.value=p.label; o.textContent=`${p.label} (${p.kmh} km/h)`; sel.appendChild(o); }
  const o=document.createElement('option'); o.value='__custom'; o.textContent='自定义…'; sel.appendChild(o);
  $('speed').value = c.default_speed_mps; $('scale').value = c.default_scale; $('repeat').value = c.default_repeat;
}
function drawTrack(points){
  if (trackLine) map.removeLayer(trackLine);
  if (startMarker) map.removeLayer(startMarker);
  trackLine = L.polyline(points, { color:'#2563eb', weight:4, opacity:.8 }).addTo(map);
  startMarker = L.circleMarker(points[0], { radius:6, color:'#16a34a', fillColor:'#16a34a', fillOpacity:1 }).addTo(map);
  map.fitBounds(trackLine.getBounds(), { padding:[30,30] });
}
async function preview(){
  if (!hasTrack) return;
  const q = new URLSearchParams(params());
  try {
    const j = await api('/api/preview?' + q.toString());
    totalPoints = j.summary.n;
    $('summary').textContent = `${j.name}: ${j.summary.n} 点 · ${j.summary.km} km · ${j.summary.dur_s} s`;
    drawTrack(j.points);
  } catch(e){ $('summary').textContent = e.message; }
}
async function upload(){
  const f = $('file').files[0]; if (!f) return;
  const buf = await f.arrayBuffer();
  await api('/api/track?name=' + encodeURIComponent(f.name), { method:'POST', body:buf });
  hasTrack = true;
  await preview();
}
function ensureMarker(lat, lon){
  if (!marker){ marker = L.circleMarker([lat,lon], { radius:9, color:'#fff', weight:2, fillColor:'#2563eb', fillOpacity:1 }).addTo(map); }
  else marker.setLatLng([lat,lon]);
  if ($('follow').checked && !map.getBounds().contains([lat,lon])) map.panTo([lat,lon]);
}

function redrawDrawn(){
  if (drawnLine) map.removeLayer(drawnLine);
  drawnDots.forEach(d => map.removeLayer(d)); drawnDots = [];
  if (drawnPts.length >= 2) drawnLine = L.polyline(drawnPts, { color:'#f59e0b', weight:3, dashArray:'6 6' }).addTo(map);
  for (const p of drawnPts) drawnDots.push(L.circleMarker(p, { radius:4, color:'#f59e0b', fillColor:'#f59e0b', fillOpacity:1 }).addTo(map));
  $('undo').disabled = !drawnPts.length; $('dclear').disabled = !drawnPts.length; $('duse').disabled = drawnPts.length < 2;
}
function setDrawing(on){
  drawing = on;
  $('draw').textContent = on ? '结束绘制' : '画轨迹';
  $('draw').classList.toggle('primary', on);
  map.getContainer().style.cursor = on ? 'crosshair' : '';
  $('drawhint').textContent = on ? '绘制中：点击地图添加点；「撤销点」回退、「用绘制轨迹」生成 GPX。'
                                 : '点「画轨迹」后在地图上逐点点击生成路线（GCJ-02），再「用绘制轨迹」。';
}
map.on('click', e => { if (!drawing) return; drawnPts.push([e.latlng.lat, e.latlng.lng]); redrawDrawn(); });
$('draw').onclick = () => setDrawing(!drawing);
$('undo').onclick = () => { drawnPts.pop(); redrawDrawn(); };
$('dclear').onclick = () => { drawnPts = []; redrawDrawn(); };
$('duse').onclick = async () => {
  try {
    await api('/api/drawn', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ points: drawnPts }) });
    hasTrack = true; setDrawing(false); drawnPts = []; redrawDrawn();
    await preview();
  } catch(e){ log(e.message); }
};
$('pause').onclick = () => api('/api/pause', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ paused: curState !== 'paused' }) }).catch(e => log(e.message));

$('refresh').onclick = refreshDevices;
$('file').onchange = upload;
$('preset').onchange = () => { if ($('preset').value !== '__custom'){ $('speed').value=''; $('scale').value=''; } preview(); };
['speed','scale','repeat','gcj'].forEach(id => $(id).onchange = preview);
$('play').onclick = async () => { try { await api('/api/play', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(params()) }); seen=0; } catch(e){ log(e.message); } };
$('stop').onclick = () => api('/api/stop', { method:'POST' }).catch(e=>log(e.message));
$('clear').onclick = () => api('/api/clear', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({udid:$('device').value}) }).catch(e=>log(e.message));

const es = new EventSource('/api/stream');
es.onmessage = ev => {
  const m = JSON.parse(ev.data);
  if (m.type === 'log') log(m.line);
  else if (m.type === 'state') setStatus(m.state);
  else if (m.type === 'point'){ seen++; ensureMarker(m.lat, m.lon); $('status').textContent = `${curState === 'paused' ? '已暂停' : '播放中'} · ${seen}/${totalPoints||'?'} · ${m.lat.toFixed(5)}, ${m.lon.toFixed(5)}`; }
};
refreshDevices(); loadConfig();
</script>
</body>
</html>
"""