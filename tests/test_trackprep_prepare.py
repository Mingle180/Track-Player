import unittest

import trackprep
from tests.trackprep_fixtures import north_line, tmp_geojson, tmp_gpx


class PrepareBaselineTest(unittest.TestCase):
    def test_speed_resample_is_1hz_and_ends_at_endpoint(self):
        line = north_line(300)
        pts = trackprep.prepare(tmp_gpx(line), speed=3.0)
        self.assertGreaterEqual(len(pts), 100)
        gaps = [(b[2] - a[2]).total_seconds() for a, b in zip(pts, pts[1:])]
        for g in gaps[:-1]:
            self.assertEqual(g, 1.0)
        # resample() appends the exact endpoint at a fractional second
        self.assertGreater(gaps[-1], 0.0)
        self.assertLessEqual(gaps[-1], 1.0)
        self.assertAlmostEqual(pts[-1][0], line[-1][0], places=6)
        self.assertAlmostEqual(pts[-1][1], line[-1][1], places=6)

    def test_scale_requires_timestamps(self):
        with self.assertRaises(SystemExit):
            trackprep.prepare(tmp_gpx(north_line(100), times=False), scale=2.0)

    def test_scale_halves_duration(self):
        path = tmp_gpx(north_line(100, steps=10), times=True)
        base = trackprep.prepare(path)
        fast = trackprep.prepare(path, scale=2.0)
        d0 = (base[-1][2] - base[0][2]).total_seconds()
        d1 = (fast[-1][2] - fast[0][2]).total_seconds()
        self.assertAlmostEqual(d1, d0 / 2, places=6)

    def test_repeat_offsets_copies(self):
        path = tmp_gpx(north_line(100, steps=10), times=True)
        one = trackprep.prepare(path)
        three = trackprep.prepare(path, repeat_n=3)
        self.assertEqual(len(three), 3 * len(one))
        dur = (one[-1][2] - one[0][2]).total_seconds()
        gap = (three[len(one)][2] - one[-1][2]).total_seconds()
        # repeat() offsets each copy by (duration + 1.0) * r → 1 s between copies
        self.assertEqual(gap, 1.0)
        self.assertAlmostEqual((three[-1][2] - three[0][2]).total_seconds(),
                               3 * dur + 2.0, places=6)

    def test_from_gcj02_shifts_by_hundreds_of_meters(self):
        line = north_line(200)
        wgs = trackprep.prepare(tmp_gpx(line), speed=3.0)
        gcj = trackprep.prepare(tmp_gpx(line), speed=3.0, from_gcj02=True)
        d = trackprep.haversine(wgs[0][0], wgs[0][1], gcj[0][0], gcj[0][1])
        self.assertGreater(d, 100.0)

    def test_geojson_resamples_by_default_speed(self):
        coords = [[lon, lat] for lat, lon in north_line(90)]
        pts = trackprep.prepare(tmp_geojson(coords))
        self.assertGreaterEqual(len(pts), 30)
        self.assertEqual((pts[1][2] - pts[0][2]).total_seconds(), 1.0)

    def test_zero_length_track_exits(self):
        with self.assertRaises(SystemExit):
            trackprep.prepare(tmp_gpx([(39.99, 116.30), (39.99, 116.30)]), speed=3.0)

    def test_script_overrides_speed_and_repeats(self):
        path = tmp_gpx(north_line(300, steps=3))
        script = [{'type': 'move', 'to_m': 150.0, 'speed': 3.0},
                  {'type': 'stay', 'seconds': 5, 'jitter': 0.0},
                  {'type': 'move', 'to_m': 300.0, 'speed': 6.0}]
        one = trackprep.prepare(path, script=script)
        three = trackprep.prepare(path, repeat_n=3, script=script)
        self.assertEqual(len(three), 3 * len(one))
        self.assertEqual((one[1][2] - one[0][2]).total_seconds(), 1.0)

    def test_script_conflicts_with_speed(self):
        with self.assertRaises(ValueError):
            trackprep.prepare(tmp_gpx(north_line(100)), speed=3.0,
                              script=[{'type': 'move', 'to_m': 50.0, 'speed': 2.0}])

    def test_script_converts_gcj_source_first(self):
        line = north_line(200)
        script = [{'type': 'move', 'to_m': 100.0, 'speed': 3.0}]
        wgs = trackprep.prepare(tmp_gpx(line), script=script)
        gcj = trackprep.prepare(tmp_gpx(line), script=script, from_gcj02=True)
        self.assertGreater(trackprep.haversine(wgs[0][0], wgs[0][1], gcj[0][0], gcj[0][1]), 100.0)


if __name__ == '__main__':
    unittest.main()