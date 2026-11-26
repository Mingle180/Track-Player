import json
import unittest
from pathlib import Path

import trackprep
from tests.trackprep_fixtures import north_line, tmp_gpx


class GeometryTest(unittest.TestCase):
    def setUp(self):
        self.line = north_line(300, steps=3)
        self.pts = trackprep.parse_track(tmp_gpx(self.line))
        self.cum = trackprep.cumulative_lengths(self.pts)

    def test_parse_track_matches_prepare_source(self):
        self.assertEqual(len(self.pts), 4)
        self.assertAlmostEqual(self.pts[0][0], 39.99, places=7)

    def test_cumulative_monotonic_and_total(self):
        self.assertEqual(self.cum[0], 0.0)
        self.assertEqual(len(self.cum), len(self.pts))
        for a, b in zip(self.cum, self.cum[1:]):
            self.assertLess(a, b)
        self.assertAlmostEqual(self.cum[-1], 300.0, delta=1.0)

    def test_point_at_endpoints_and_midpoint(self):
        self.assertAlmostEqual(trackprep.point_at(self.pts, self.cum, 0.0)[0], 39.99, places=7)
        self.assertAlmostEqual(trackprep.point_at(self.pts, self.cum, self.cum[-1])[0],
                               self.line[-1][0], places=7)
        mid = trackprep.point_at(self.pts, self.cum, self.cum[-1] / 2)
        self.assertAlmostEqual(mid[0], (self.line[0][0] + self.line[-1][0]) / 2, places=6)

    def test_point_at_clamps_out_of_range(self):
        self.assertEqual(trackprep.point_at(self.pts, self.cum, -50),
                         trackprep.point_at(self.pts, self.cum, 0.0))
        self.assertEqual(trackprep.point_at(self.pts, self.cum, 1e9),
                         trackprep.point_at(self.pts, self.cum, self.cum[-1]))

    def test_slice_track_endpoints_exact(self):
        sub = trackprep.slice_track(self.pts, self.cum, 50.0, 200.0)
        self.assertAlmostEqual(sub[0][0], trackprep.point_at(self.pts, self.cum, 50.0)[0], places=9)
        self.assertAlmostEqual(sub[-1][0], trackprep.point_at(self.pts, self.cum, 200.0)[0], places=9)
        self.assertAlmostEqual(trackprep.cumulative_lengths(sub)[-1], 150.0, delta=1.0)


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.pts = trackprep.parse_track(tmp_gpx(north_line(300, steps=3)))
        self.cum = trackprep.cumulative_lengths(self.pts)

    def test_point_on_track_snaps_to_itself(self):
        lat, lon = trackprep.point_at(self.pts, self.cum, 150.0)
        along, slat, slon = trackprep.project_point(self.pts, self.cum, lat, lon)
        self.assertAlmostEqual(along, 150.0, delta=1.0)
        self.assertAlmostEqual(slat, lat, places=7)
        self.assertAlmostEqual(slon, lon, places=7)

    def test_offset_point_projects_perpendicularly(self):
        lat, lon = trackprep.point_at(self.pts, self.cum, 150.0)
        along, slat, slon = trackprep.project_point(self.pts, self.cum, lat, lon + 0.001)
        self.assertAlmostEqual(along, 150.0, delta=1.0)
        self.assertAlmostEqual(slon, lon, places=6)

    def test_before_start_clamps_to_zero(self):
        along, _, _ = trackprep.project_point(self.pts, self.cum, 39.98, 116.30)
        self.assertAlmostEqual(along, 0.0, delta=1.0)

    def test_after_end_clamps_to_total(self):
        along, _, _ = trackprep.project_point(self.pts, self.cum, 40.01, 116.30)
        self.assertAlmostEqual(along, self.cum[-1], delta=1.0)


class CompileScriptTest(unittest.TestCase):
    def setUp(self):
        self.line = north_line(300, steps=3)
        self.pts = trackprep.parse_track(tmp_gpx(self.line))

    def test_two_legs_have_different_step_lengths(self):
        out = trackprep.compile_script(self.pts, [
            {'type': 'move', 'to_m': 100.0, 'speed': 3.0},
            {'type': 'move', 'to_m': 300.0, 'speed': 6.0},
        ])
        steps = [trackprep.haversine(a[0], a[1], b[0], b[1]) for a, b in zip(out, out[1:])]
        # Each leg's last step is the sub-speed remainder (100 - 33*3 = 1 m), so
        # probe internal steps rather than the leg boundaries.
        self.assertAlmostEqual(steps[1], 3.0, delta=0.2)
        self.assertAlmostEqual(steps[40], 6.0, delta=0.2)
        self.assertAlmostEqual(out[-1][0], self.line[-1][0], places=6)

    def test_timestamps_are_strictly_1hz(self):
        out = trackprep.compile_script(self.pts, [
            {'type': 'move', 'to_m': 120.0, 'speed': 3.0},
            {'type': 'stay', 'seconds': 5, 'jitter': 0.0},
            {'type': 'move', 'to_m': 300.0, 'speed': 5.0},
        ])
        self.assertEqual(out[0][2], trackprep.BASE_TIME)
        for a, b in zip(out, out[1:]):
            self.assertEqual((b[2] - a[2]).total_seconds(), 1.0)

    def test_stay_zero_jitter_is_identical_coords(self):
        out = trackprep.compile_script(self.pts, [
            {'type': 'move', 'to_m': 100.0, 'speed': 5.0},
            {'type': 'stay', 'seconds': 30, 'jitter': 0.0},
        ])
        tail = out[-30:]
        self.assertEqual(len({(p[0], p[1]) for p in tail}), 1)

    def test_stay_jitter_stays_within_4_sigma(self):
        out = trackprep.compile_script(self.pts, [
            {'type': 'stay', 'seconds': 200, 'jitter': 1.0, 'seed': 7},
        ])
        anchor = trackprep.point_at(self.pts, trackprep.cumulative_lengths(self.pts), 0.0)
        for lat, lon, _ in out[1:]:
            d = trackprep.haversine(anchor[0], anchor[1], lat, lon)
            self.assertLess(d, 6.0)
        self.assertGreater(len({(p[0], p[1]) for p in out}), 1)

    def test_stay_is_reproducible_with_seed(self):
        script = [{'type': 'stay', 'seconds': 20, 'jitter': 1.0, 'seed': 3}]
        a = trackprep.compile_script(self.pts, script)
        b = trackprep.compile_script(self.pts, script)
        self.assertEqual(a, b)

    def test_stay_at_start_then_move(self):
        out = trackprep.compile_script(self.pts, [
            {'type': 'stay', 'seconds': 10, 'jitter': 0.0},
            {'type': 'move', 'to_m': 300.0, 'speed': 10.0},
        ])
        self.assertAlmostEqual(out[0][0], self.line[0][0], places=7)
        self.assertAlmostEqual(out[10][0], self.line[0][0], places=7)
        self.assertAlmostEqual(out[-1][0], self.line[-1][0], places=6)

    def test_short_move_and_remainder_each_get_a_tick(self):
        total = trackprep.cumulative_lengths(self.pts)[-1]
        out = trackprep.compile_script(self.pts, [
            {'type': 'move', 'to_m': 1, 'speed': 10},
            {'type': 'move', 'to_m': total, 'speed': 100},
            {'type': 'stay', 'seconds': 2},
        ])
        self.assertEqual(len(out), 7)  # Start, 1 + 3 move ticks, 2 hold ticks.
        self.assertEqual([p[:2] for p in out[-3:]], [self.pts[-1][:2]] * 3)

    def test_rejects_invalid_script_values_before_allocating_points(self):
        for leg in ({'type': 'stay', 'seconds': 1.2}, {'type': 'stay', 'seconds': True},
                    {'type': 'stay', 'seconds': 1, 'seed': 2.5},
                    {'type': 'stay', 'seconds': 1, 'jitter': float('nan')},
                    {'type': 'move', 'to_m': 2, 'speed': float('inf')},
                    {'type': 'move', 'to_m': 2, 'speed': 1e-300},
                    {'type': 'stay', 'seconds': 10**20}):
            with self.subTest(leg=leg), self.assertRaisesRegex(ValueError, r'leg 1'):
                trackprep.compile_script(self.pts, [leg])
        with self.assertRaisesRegex(ValueError, 'point script limit'):
            trackprep.compile_script(self.pts, [{'type': 'stay', 'seconds': 5}], repeat_n=10**9)

    def test_documented_demo_compiles_with_bundled_route(self):
        examples = Path(__file__).resolve().parents[1] / 'examples'
        script = json.loads((examples / 'legs_demo.json').read_text(encoding='utf-8'))
        points = trackprep.prepare(examples / 'pku54_loop.gpx', script=script)
        self.assertEqual(len(points), 261)

    def test_rejects_non_increasing_to_m(self):
        with self.assertRaises(ValueError):
            trackprep.compile_script(self.pts, [
                {'type': 'move', 'to_m': 200.0, 'speed': 3.0},
                {'type': 'move', 'to_m': 100.0, 'speed': 3.0},
            ])

    def test_rejects_to_m_beyond_total(self):
        with self.assertRaises(ValueError):
            trackprep.compile_script(self.pts, [{'type': 'move', 'to_m': 9999.0, 'speed': 3.0}])

    def test_rejects_bad_speed_seconds_jitter_type(self):
        for bad in ([{'type': 'move', 'to_m': 100.0, 'speed': 0}],
                    [{'type': 'stay', 'seconds': 0}],
                    [{'type': 'stay', 'seconds': 5, 'jitter': -1}],
                    [{'type': 'fly', 'to_m': 100.0}],
                    [],
                    [{'type': 'move', 'to_m': 'x', 'speed': 3}]):
            with self.assertRaises(ValueError):
                trackprep.compile_script(self.pts, bad)


if __name__ == '__main__':
    unittest.main()
