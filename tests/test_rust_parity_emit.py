"""Differential tests for step 5 of the Rust port: plan-to-G-code emission
(`_rdp_indices`, `_simplify_paired`, `_line_to`, `_travel_to`, `_emit_plan`)
in `gcode_layer_interval_detour.py`.

Unlike step 4's targets, these are real importable module-level functions,
so this test calls the actual production code directly.
"""

import random
import unittest

try:
    import fuzzypaint_engine
except ImportError as exc:  # pragma: no cover - environment guard
    raise unittest.SkipTest(
        "fuzzypaint_engine is not built/installed in this interpreter; "
        "run `maturin develop` inside fuzzypaint_engine/.venv first"
    ) from exc

from gcode_layer_detour import Segment
from gcode_layer_interval_detour import Span, SegmentPlan, _emit_plan, _rdp_indices


def _make_segment(x1, y1, x2, y2, filament=0, object_id=1, line_number=1, command=b"G1", extrusion=0.2, feed=None):
    return Segment(x1, y1, x2, y2, filament, object_id, line_number, command, extrusion, 0.0, 0.0, feed)


class FormatGParityTests(unittest.TestCase):
    def test_matches_python_g_format_over_typical_and_edge_values(self):
        values = [
            0.0, 1.0, 1800.0, 3000.0, 9000.0, 30000.0, 100.0, 0.5, 100.25,
            0.1, 0.123456789, 1234567.0, 0.00001234, 999999.0, 1000000.0,
            -1800.0, -0.5, 123456.789, 1e-5, 1e6, 1e20, 1e-20, 2.5,
        ]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(f"{value:g}", fuzzypaint_engine.format_g(value))

    def test_random_values(self):
        random.seed(20261111)
        for _ in range(500):
            value = random.uniform(-100000, 100000)
            self.assertEqual(f"{value:g}", fuzzypaint_engine.format_g(value))


class RdpIndicesParityTests(unittest.TestCase):
    def test_straight_line_collapses(self):
        points = [(0.0, 0.0), (1.0, 0.001), (2.0, -0.002), (3.0, 0.0)]
        self.assertEqual(_rdp_indices(points, 0.01), fuzzypaint_engine.rdp_indices(points, 0.01))

    def test_real_corner_is_kept(self):
        points = [(0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (3.0, 1.0), (4.0, 0.0)]
        self.assertEqual(_rdp_indices(points, 0.01), fuzzypaint_engine.rdp_indices(points, 0.01))

    def test_random_polylines(self):
        random.seed(20261111)
        for _ in range(50):
            points = [(random.uniform(-10, 10), random.uniform(-10, 10)) for _ in range(random.randint(2, 30))]
            self.assertEqual(_rdp_indices(points, 0.05), fuzzypaint_engine.rdp_indices(points, 0.05))


DEFAULT_PARAMS = dict(
    inset_mm=0.24,
    boundary_resolution_mm=0.10,
    transition_distance_mm=0.55,
    endpoint_coverage_mm=0.21,
    inner_search_mm=1.2,
    minimum_span_mm=0.10,
    cell_mm=0.55,
)


def _as_tuple(segment: Segment):
    return (
        segment.x1, segment.y1, segment.x2, segment.y2, segment.filament, segment.object_id,
        segment.line_number, segment.command, segment.extrusion, segment.i, segment.j, segment.feed,
    )


class EmitPlanParityTests(unittest.TestCase):
    def test_simple_detour_matches_byte_for_byte(self):
        segment = _make_segment(0.0, 0.0, 5.0, 0.0, line_number=10, feed=1800.0)
        plan = SegmentPlan(
            segment=segment,
            original=[(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0), (5.0, 0.0)],
            spans=[Span(
                original=[(2.0, 0.0), (3.0, 0.0)],
                shifted=[(2.0, 0.1), (3.0, 0.1)],
            )],
        )
        python_result = _emit_plan(plan)
        self.assertEqual(python_result, _direct_rust_emit(plan, segment))

    def test_no_feed_pending(self):
        segment = _make_segment(0.0, 0.0, 5.0, 0.0, line_number=10, feed=None)
        plan = SegmentPlan(
            segment=segment,
            original=[(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)],
            spans=[Span(original=[(1.0, 0.0), (2.0, 0.0)], shifted=[(1.0, 0.1), (2.0, 0.1)])],
        )
        python_result = _emit_plan(plan)
        self.assertEqual(python_result, _direct_rust_emit(plan, segment))

    def test_arc_segment_command(self):
        segment = Segment(10.0, 0.0, 0.0, 10.0, 0, 1, 10, b"G2", 0.3, -10.0, 0.0, 3000.0)
        plan = SegmentPlan(
            segment=segment,
            original=[(10.0, 0.0), (9.0, 4.0), (5.0, 8.0), (0.0, 10.0)],
            spans=[Span(original=[(9.0, 4.0), (5.0, 8.0)], shifted=[(8.5, 3.8), (4.8, 7.6)])],
        )
        python_result = _emit_plan(plan)
        self.assertEqual(python_result, _direct_rust_emit(plan, segment))

    def test_continue_in_and_out_suppress_travel_moves(self):
        segment = _make_segment(0.0, 0.0, 4.0, 0.0, line_number=10)
        plan = SegmentPlan(
            segment=segment,
            original=[(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0), (4.0, 0.0)],
            spans=[Span(
                original=[(1.0, 0.0), (2.0, 0.0), (3.0, 0.0)],
                shifted=[(1.0, 0.0), (2.0, 0.1), (3.0, 0.0)],
                continue_in=True,
                continue_out=True,
            )],
        )
        python_result = _emit_plan(plan)
        self.assertEqual(python_result, _direct_rust_emit(plan, segment))

    def test_many_points_triggers_simplify_paired(self):
        segment = _make_segment(0.0, 0.0, 10.0, 0.0, line_number=10)
        original_span = [(float(i) * 0.5, 0.0) for i in range(21)]
        shifted_span = [(float(i) * 0.5, 0.1 if 0 < i < 20 else 0.0) for i in range(21)]
        plan = SegmentPlan(
            segment=segment,
            original=[(0.0, 0.0)] + original_span + [(10.0, 0.0)],
            spans=[Span(original=original_span, shifted=shifted_span)],
        )
        python_result = _emit_plan(plan)
        self.assertEqual(python_result, _direct_rust_emit(plan, segment))


def _direct_rust_emit(plan, segment):
    """Calls the Rust `_emit_plan` port directly via the test-only module
    attribute, bypassing `plan_segment` (which `plan_and_emit_segment`
    otherwise re-derives) since this test supplies a hand-built plan."""
    return fuzzypaint_engine.emit_plan(
        _as_tuple(segment),
        plan.original,
        [
            (span.original, span.shifted, span.continue_in, span.continue_out)
            for span in plan.spans
        ],
    )


if __name__ == "__main__":
    unittest.main()
