"""Differential tests for step 3 of the Rust port: the spatial grid and
neighbor search (`_sample_points`, `_polyline_length`, `_closest_point`,
`_aligned`, `_grid`, `_nearby_point`) in `gcode_layer_detour.py`.

Unlike steps 1-2, these are real importable module-level functions (not
inlined in a closure), so this test calls the actual production code
directly -- no duplicated reference implementation needed.
"""

import math
import random
import unittest

try:
    import fuzzypaint_engine
except ImportError as exc:  # pragma: no cover - environment guard
    raise unittest.SkipTest(
        "fuzzypaint_engine is not built/installed in this interpreter; "
        "run `maturin develop` inside fuzzypaint_engine/.venv first"
    ) from exc

from gcode_layer_detour import Segment, _aligned, _closest_point, _grid, _nearby_point, _polyline_length, _sample_points


def _make_segment(x1, y1, x2, y2, filament=0, object_id=1, line_number=1, command=b"G1", extrusion=0.1, i=0.0, j=0.0, feed=None):
    return Segment(x1, y1, x2, y2, filament, object_id, line_number, command, extrusion, i, j, feed)


def _as_tuple(segment: Segment):
    return (
        segment.x1, segment.y1, segment.x2, segment.y2, segment.filament, segment.object_id,
        segment.line_number, segment.command, segment.extrusion, segment.i, segment.j, segment.feed,
    )


class ClosestPointAndAlignedParityTests(unittest.TestCase):
    def test_closest_point_matches_for_various_points(self):
        segment = _make_segment(0.0, 0.0, 10.0, 0.0)
        for px, py in [(-5.0, 0.0), (15.0, 2.0), (5.0, 3.0), (5.0, -3.0), (5.0, 0.0)]:
            python_result = _closest_point(px, py, segment)
            rust_result = fuzzypaint_engine.closest_point(px, py, _as_tuple(segment))
            self.assertEqual(python_result, rust_result)

    def test_closest_point_on_zero_length_segment(self):
        segment = _make_segment(3.0, 4.0, 3.0, 4.0)
        python_result = _closest_point(10.0, 0.0, segment)
        rust_result = fuzzypaint_engine.closest_point(10.0, 0.0, _as_tuple(segment))
        self.assertEqual(python_result, rust_result)

    def test_aligned_matches_for_various_angles(self):
        a = _make_segment(0.0, 0.0, 10.0, 0.0)
        cases = [
            _make_segment(0.0, 5.0, 10.0, 5.0),  # parallel
            _make_segment(0.0, 0.0, 0.0, 10.0),  # perpendicular
            _make_segment(0.0, 0.0, 10.0, 10.0),  # 45 degrees
            _make_segment(5.0, 5.0, 5.0, 5.0),  # zero length
            _make_segment(0.0, 0.0, -10.0, 0.0),  # anti-parallel
        ]
        for b in cases:
            for minimum in (0.35, 0.45, 0.75):
                python_result = _aligned(a, b, minimum)
                rust_result = fuzzypaint_engine.aligned(_as_tuple(a), _as_tuple(b), minimum)
                self.assertEqual(python_result, rust_result)


class SamplePointsParityTests(unittest.TestCase):
    def test_straight_line_sampling(self):
        segment = _make_segment(0.0, 0.0, 3.3, 0.0)
        python_points = _sample_points(segment, 0.1)
        rust_points = fuzzypaint_engine.sample_points(_as_tuple(segment), 0.1)
        self.assertEqual(python_points, rust_points)

    def test_arc_sampling_g2(self):
        segment = _make_segment(10.0, 0.0, 0.0, 10.0, command=b"G2", i=-10.0, j=0.0)
        python_points = _sample_points(segment, 0.1)
        rust_points = fuzzypaint_engine.sample_points(_as_tuple(segment), 0.1)
        self.assertEqual(len(python_points), len(rust_points))
        for (px, py), (rx, ry) in zip(python_points, rust_points):
            self.assertAlmostEqual(px, rx, places=9)
            self.assertAlmostEqual(py, ry, places=9)

    def test_arc_sampling_g3(self):
        segment = _make_segment(10.0, 0.0, 0.0, -10.0, command=b"G3", i=-10.0, j=0.0)
        python_points = _sample_points(segment, 0.1)
        rust_points = fuzzypaint_engine.sample_points(_as_tuple(segment), 0.1)
        self.assertEqual(len(python_points), len(rust_points))
        for (px, py), (rx, ry) in zip(python_points, rust_points):
            self.assertAlmostEqual(px, rx, places=9)
            self.assertAlmostEqual(py, ry, places=9)

    def test_degenerate_arc_falls_back_to_two_points(self):
        segment = _make_segment(1.0, 1.0, 2.0, 2.0, command=b"G2", i=0.0, j=0.0)
        python_points = _sample_points(segment, 0.1)
        rust_points = fuzzypaint_engine.sample_points(_as_tuple(segment), 0.1)
        self.assertEqual(python_points, rust_points)

    def test_polyline_length(self):
        points = [(0.0, 0.0), (3.0, 0.0), (3.0, 4.0), (0.0, 4.0)]
        self.assertEqual(_polyline_length(points), fuzzypaint_engine.polyline_length(points))


class GridAndNearbyPointParityTests(unittest.TestCase):
    def _assert_same_nearby(self, segments, object_id, x, y, radius_mm, cell_mm):
        grid = _grid(segments, cell_mm)
        python_found = [s.line_number for s in _nearby_point(grid, object_id, x, y, radius_mm, cell_mm)]
        rust_found = fuzzypaint_engine.nearby_segment_line_numbers(
            [_as_tuple(s) for s in segments], cell_mm, object_id, x, y, radius_mm
        )
        self.assertEqual(python_found, rust_found)

    def test_finds_segments_within_radius_and_excludes_other_objects(self):
        segments = [
            _make_segment(0.0, 0.0, 1.0, 0.0, object_id=1, line_number=1),
            _make_segment(0.0, 10.0, 1.0, 10.0, object_id=1, line_number=2),
            _make_segment(0.0, 0.0, 1.0, 0.0, object_id=2, line_number=3),
        ]
        self._assert_same_nearby(segments, object_id=1, x=0.5, y=0.0, radius_mm=1.2, cell_mm=0.55)

    def test_dedup_order_matches_python_generator(self):
        # Several segments whose sample points land in overlapping cells --
        # the dedup-by-line_number order must match exactly.
        segments = [
            _make_segment(0.0, 0.0, 2.0, 0.0, object_id=1, line_number=5),
            _make_segment(0.0, 0.3, 2.0, 0.3, object_id=1, line_number=3),
            _make_segment(0.0, 0.6, 2.0, 0.6, object_id=1, line_number=9),
            _make_segment(0.0, 0.9, 2.0, 0.9, object_id=1, line_number=1),
        ]
        self._assert_same_nearby(segments, object_id=1, x=1.0, y=0.45, radius_mm=1.0, cell_mm=0.55)

    def test_random_segment_cloud_matches_exactly(self):
        random.seed(20261111)
        segments = []
        for line_number in range(1, 400):
            x1 = random.uniform(-20, 20)
            y1 = random.uniform(-20, 20)
            angle = random.uniform(0, 6.28318)
            length = random.uniform(0.1, 3.0)
            x2 = x1 + length * math.cos(angle)
            y2 = y1 + length * math.sin(angle)
            object_id = random.choice([1, 1, 1, 2])
            segments.append(_make_segment(x1, y1, x2, y2, object_id=object_id, line_number=line_number))
        for _ in range(20):
            x = random.uniform(-20, 20)
            y = random.uniform(-20, 20)
            radius = random.uniform(0.5, 4.0)
            self._assert_same_nearby(segments, object_id=1, x=x, y=y, radius_mm=radius, cell_mm=0.55)


if __name__ == "__main__":
    unittest.main()
