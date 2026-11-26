import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import trackprep
import trackplayer_store as store
import trackplayer_web as web
from tests.trackprep_fixtures import north_line, tmp_gpx


class WebApiCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='tp_store_')
        self.state = web.State()
        self.state.store_root = self.root
        self._real_devices = web._devices
        web._devices = lambda: []
        self.server = web._HTTPServer(('127.0.0.1', 0), web._make_handler(self.state))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.state.cleanup()
        web._devices = self._real_devices
        shutil.rmtree(self.root, ignore_errors=True)

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path):
        with urllib.request.urlopen(self.url(path), timeout=10) as r:
            return json.loads(r.read())

    def post(self, path, obj=None, raw=None):
        data = raw if raw is not None else json.dumps({} if obj is None else obj).encode('utf-8')
        req = urllib.request.Request(self.url(path), data=data, method='POST',
                                     headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read())

    def upload(self, points=None):
        path = tmp_gpx(points if points is not None else north_line(300))
        with open(path, 'rb') as f:
            data = f.read()
        return self.post('/api/track?name=t.gpx', raw=data)


class ExistingContractTest(WebApiCase):
    def test_devices_shape(self):
        j = self.get('/api/devices')
        self.assertEqual(j['devices'], [])
        self.assertIsNone(j['last_point'])

    def test_config_shape(self):
        j = self.get('/api/config')
        self.assertIn('presets', j)
        self.assertIn('default_speed_mps', j)

    def test_preview_without_upload_is_400(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get('/api/preview?speed=3')
        with cm.exception:
            self.assertEqual(cm.exception.code, 400)

    def test_upload_then_preview(self):
        code, j = self.upload()
        self.assertEqual((code, j['ok']), (200, True))
        p = self.get('/api/preview?speed=3')
        self.assertEqual(p['name'], 't.gpx')
        self.assertGreater(p['summary']['n'], 50)
        self.assertEqual(len(p['points']), p['summary']['n'])

    def test_play_without_upload_is_400(self):
        code, j = self.post('/api/play', {})
        self.assertEqual(code, 400)
        self.assertIn('no track uploaded', j['error'])

    def test_pause_when_idle_is_400(self):
        code, j = self.post('/api/pause', {'paused': True})
        self.assertEqual(code, 400)

    def test_clear_without_device_is_400(self):
        with patch.object(web.tp, 'list_devices', return_value=[]):
            code, j = self.post('/api/clear', {})
        self.assertEqual(code, 400)
        self.assertTrue(j['error'])

    def test_drawn_roundtrip(self):
        code, j = self.post('/api/drawn', {'points': [[39.99, 116.30], [39.991, 116.30],
                                                      [39.992, 116.30]]})
        self.assertEqual((code, j['ok']), (200, True))
        p = self.get('/api/preview?speed=3')
        self.assertGreater(p['summary']['n'], 10)

    def test_unknown_route_is_404(self):
        code, j = self.post('/api/nope', {})
        self.assertEqual(code, 404)

    def test_windows_disconnect_does_not_retry_response(self):
        handler = self.server.RequestHandlerClass
        with patch.object(handler, '_json', side_effect=ConnectionAbortedError) as respond:
            with self.assertRaises(http.client.RemoteDisconnected):
                self.get('/api/devices')
            with self.assertRaises(http.client.RemoteDisconnected):
                self.post('/api/stop')
        self.assertEqual(respond.call_count, 2)


class LastPointPersistenceTest(WebApiCase):
    def test_remember_throttles_disk_writes(self):
        self.state.save_interval = 3600.0
        # monotonic() is system uptime here, so prime the throttle clock: otherwise
        # the very first remember() is already "due" and flushes immediately.
        self.state._last_save = time.monotonic()
        self.state.remember(39.99, 116.30)
        self.assertIsNone(store.load_last_point(self.root))
        self.state.remember(39.991, 116.301)
        self.assertIsNone(store.load_last_point(self.root))
        self.assertEqual(self.state.last_point, (39.991, 116.301))

    def test_flush_writes_and_devices_returns_it(self):
        self.state.remember(39.99, 116.30)
        self.state.flush_last_point()
        self.assertEqual(store.load_last_point(self.root), [39.99, 116.30])
        self.assertEqual(self.get('/api/devices')['last_point'], [39.99, 116.30])

    def test_serve_loads_persisted_point(self):
        store.save_last_point(39.98, 116.31, root=self.root)
        captured = {}
        real = web._HTTPServer

        class Capture(real):
            def __init__(self, addr, handler):
                super().__init__(addr, handler)
                captured['server'] = self

        web._HTTPServer = Capture
        t = threading.Thread(target=web.serve, daemon=True,
                             kwargs={'port': 0, 'open_browser': False, 'store_root': self.root})
        t.start()
        for _ in range(100):
            if 'server' in captured:
                break
            time.sleep(0.02)
        try:
            self.assertIn('server', captured)
            port = captured['server'].server_address[1]
            with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/devices', timeout=10) as r:
                j = json.loads(r.read())
            self.assertEqual(j['last_point'], [39.98, 116.31])
        finally:
            if 'server' in captured:
                captured['server'].shutdown()
            t.join(timeout=5)
            web._HTTPServer = real

    def test_forget_clears_memory_and_disk(self):
        self.state.remember(39.99, 116.30)
        self.state.flush_last_point()
        with patch.object(web.tp, 'pick_device', return_value='test'), patch.object(web.tp, 'restore') as restore:
            self.assertEqual(self.post('/api/clear'), (200, {'ok': True}))
        restore.assert_called_once_with('test')
        self.assertIsNone(self.state.last_point)
        self.assertIsNone(store.load_last_point(self.root))
        self.assertIsNone(self.get('/api/devices')['last_point'])

    def test_stop_flushes_latest_point_and_releases_replaced_source(self):
        self.upload()
        self.state.save_interval = 3600
        original = Path(self.state.upload_path)
        expected = trackprep.wgs84_to_gcj02(39.991, 116.301)
        ready = threading.Event()
        events = self.state.hub.subscribe()
        session = {}

        def play(path, *, stop_event, pause_event, on_point, **kwargs):
            session['pause'] = pause_event
            on_point(39.99, 116.30)
            on_point(39.991, 116.301)
            ready.set()
            stop_event.wait(5)
            session['source_live_on_stop'] = Path(path).is_file()

        with patch.object(web.tp, 'play_track', side_effect=play):
            self.assertEqual(self.post('/api/play')[0], 200)
            try:
                self.assertTrue(ready.wait(2))
                self.assertEqual(self.post('/api/play')[0], 400)
                self.assertEqual(self.post('/api/pause', {'paused': True})[0], 200)
                self.assertTrue(session['pause'].is_set())
                self.assertEqual(self.post('/api/pause', {'paused': False})[0], 200)
                self.assertFalse(session['pause'].is_set())
                self.assertNotEqual(store.load_last_point(self.root), list(expected))
                self.assertEqual(self.upload(north_line(250))[0], 200)
                self.assertTrue(original.exists())
            finally:
                self.post('/api/stop')
                while events.get(timeout=2).get('state') != 'idle':
                    pass
        self.assertTrue(session['source_live_on_stop'])
        self.assertFalse(original.exists())
        self.assertEqual(store.load_last_point(self.root), list(expected))


class ScriptAndLibraryTest(WebApiCase):
    SCRIPT = [{'type': 'move', 'to_m': 100.0, 'speed': 3.0},
              {'type': 'stay', 'seconds': 5, 'jitter': 0.0},
              {'type': 'move', 'to_m': 300.0, 'speed': 6.0}]

    def test_post_preview_with_script(self):
        self.upload()
        code, j = self.post('/api/preview', {'script': self.SCRIPT})
        self.assertEqual(code, 200)
        self.assertIn('source_m', j)
        self.assertAlmostEqual(j['source_m'], 300.0, delta=2.0)
        self.assertEqual(len(j['leg_marks']), 2)
        self.assertGreater(j['summary']['n'], 40)

    def test_get_preview_unchanged_without_script(self):
        self.upload()
        j = self.get('/api/preview?speed=3')
        self.assertNotIn('source_m', j)
        self.assertNotIn('leg_marks', j)

    def test_preview_rejects_bad_script(self):
        self.upload()
        code, j = self.post('/api/preview', {'script': [{'type': 'move', 'to_m': 9e9, 'speed': 3}]})
        self.assertEqual(code, 400)
        self.assertIn('to_m', j['error'])

    def test_project_snaps_to_track(self):
        self.upload()
        code, j = self.post('/api/script/project', {'lat': 39.9913, 'lon': 116.301, 'from_gcj02': 0})
        self.assertEqual(code, 200)
        self.assertAlmostEqual(j['total_m'], 300.0, delta=2.0)
        self.assertGreaterEqual(j['at_m'], 0.0)
        self.assertLessEqual(j['at_m'], j['total_m'])

    def test_project_without_upload_is_400(self):
        code, j = self.post('/api/script/project', {'lat': 39.99, 'lon': 116.30})
        self.assertEqual(code, 400)

    def test_library_save_list_load_update_delete(self):
        self.upload()
        code, saved = self.post('/api/tracks/save', {'name': 'loop', 'from_gcj02': 0, 'script': self.SCRIPT})
        self.assertEqual(code, 200)
        tid = saved['id']
        listed = self.get('/api/tracks')['tracks']
        self.assertEqual([t['id'] for t in listed], [tid])
        self.assertTrue(listed[0]['has_script'])
        code, loaded = self.post('/api/tracks/load', {'id': tid})
        self.assertEqual(code, 200)
        self.assertEqual(loaded['script'], self.SCRIPT)
        self.assertEqual(self.state.upload_name, 'loop')
        code, _ = self.post('/api/tracks/update', {'id': tid, 'name': 'renamed'})
        self.assertEqual(code, 200)
        self.assertEqual(self.post('/api/tracks/load', {'id': tid})[1]['script'], self.SCRIPT)
        code, _ = self.post('/api/tracks/update', {'id': tid, 'script': None})
        self.assertEqual(code, 200)
        self.assertFalse(self.get('/api/tracks')['tracks'][0]['has_script'])
        code, _ = self.post('/api/tracks/delete', {'id': tid})
        self.assertEqual(code, 200)
        self.assertEqual(self.get('/api/tracks')['tracks'], [])

    def test_library_load_restores_gcj_flag(self):
        self.upload()
        _, saved = self.post('/api/tracks/save', {'name': 'amap', 'from_gcj02': 1})
        _, loaded = self.post('/api/tracks/load', {'id': saved['id']})
        self.assertTrue(loaded['from_gcj02'])

    def test_library_errors_are_400(self):
        self.assertEqual(self.post('/api/tracks/save', {'name': 'x'})[0], 400)
        self.assertEqual(self.post('/api/tracks/load', {'id': 'ffffffffffff'})[0], 400)
        self.assertEqual(self.post('/api/tracks/delete', {'id': '../x'})[0], 400)

    def test_play_accepts_script_param(self):
        self.upload()
        invoked = threading.Event()
        with patch.object(web.tp, 'play_track', side_effect=lambda *args, **kwargs: invoked.set()) as play:
            code, j = self.post('/api/play', {'script': self.SCRIPT, 'duration': 1})
            self.assertTrue(invoked.wait(2))
            self.assertEqual(play.call_args.kwargs['script'], self.SCRIPT)
        self.assertEqual((code, j['ok']), (200, True))

    def test_invalid_script_does_not_replace_saved_script(self):
        self.upload()
        _, saved = self.post('/api/tracks/save', {'script': self.SCRIPT})
        bad = [{'type': 'stay', 'seconds': 1.5}]
        self.assertEqual(self.post('/api/tracks/update', {'id': saved['id'], 'script': bad})[0], 400)
        self.assertEqual(self.post('/api/play', {'script': bad})[0], 400)
        self.assertEqual(self.post('/api/tracks/load', {'id': saved['id']})[1]['script'], self.SCRIPT)

    def test_projected_endpoint_can_be_used_as_a_move_target(self):
        self.upload()
        code, j = self.post('/api/preview', {'include_source': True})
        self.assertEqual(code, 200)
        self.assertEqual(len(j['source_points']), 11)
        code, projected = self.post('/api/script/project', {'lat': 40.01, 'lon': 116.302})
        self.assertEqual(code, 200)
        self.assertEqual(projected['at_m'], j['source_m'])
        script = [{'type': 'move', 'to_m': projected['at_m'], 'speed': 3}]
        code, preview = self.post('/api/preview', {'script': script})
        self.assertEqual(code, 200)
        self.assertEqual(preview['points'][-1], j['source_points'][-1])


if __name__ == '__main__':
    unittest.main()
