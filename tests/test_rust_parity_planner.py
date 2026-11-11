"""Differential tests for step 4 of the Rust port: the detour-planning core
nested inside `gcode_layer_interval_detour.build_interval_detours` --
`shift_point`, `point_has_opposite`, and `plan_segment`.

These are closures in the Python source (they capture `inset_mm`,
`boundary_resolution_mm`, etc. from `build_interval_detours`'s own keyword
arguments), so -- like step 2's target, and unlike step 3's -- there is no
standalone function to import. `_reference_shift_point`,
`_reference_point_has_opposite`, and `_reference_plan_segment` below are
labelled, deliberate copies with the captured config turned into explicit
parameters; everything else they call (`_aligned`, `_nearby_point`,
`_sample_points`, `_polyline_length`, `_distance`) is imported directly from
production code (those are real module-level functions, proven in step 3).
Keep these three in sync with the real closures until step 5 replaces the
whole pipeline and this file can be deleted.
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

from gcode_layer_detour import Segment, _aligned, _grid, _nearby_point, _polyline_length, _sample_points
from gcode_layer_interval_detour import _distance


def _reference_shift_point(point, segment, inner_grid, *, inset_mm, inner_search_mm, cell_mm):
    px, py = point
    same_best = any_best = None
    same_distance2 = any_distance2 = inner_search_mm * inner_search_mm
    for candidate in _nearby_point(inner_grid, segment.object_id, px, py, inner_search_mm, cell_mm):
        if not _aligned(segment, candidate):
            continue
        if candidate.length2 < 1e-12:
            qx, qy = candidate.x1, candidate.y1
        else:
            projection = max(
                0.0,
                min(1.0, ((px - candidate.x1) * candidate.dx + (py - candidate.y1) * candidate.dy) / candidate.length2),
            )
            qx = candidate.x1 + projection * candidate.dx
            qy = candidate.y1 + projection * candidate.dy
        distance2 = (px - qx) ** 2 + (py - qy) ** 2
        if 0.0025 < distance2 < any_distance2:
            any_best, any_distance2 = (qx, qy), distance2
        if candidate.filament == segment.filament and 0.0025 < distance2 < same_distance2:
            same_best, same_distance2 = (qx, qy), distance2
    best = same_best if same_best is not None else any_best
    if best is None:
        return None
    best_distance = math.sqrt(same_distance2 if same_best is not None else any_distance2)
    amount = min(inset_mm, best_distance * 0.72)
    qx, qy = best
    return (px + (qx - px) * amount / best_distance, py + (qy - py) * amount / best_distance)


def _reference_point_has_opposite(
    point, segment, reference_grid, *, transition_distance_mm, boundary_resolution_mm, endpoint_coverage_mm, cell_mm
):
    px, py = point
    for candidate in _nearby_point(reference_grid, segment.object_id, px, py, transition_distance_mm, cell_mm):
        if candidate.length2 < 1e-12:
            continue
        projection = ((px - candidate.x1) * candidate.dx + (py - candidate.y1) * candidate.dy) / candidate.length2
        endpoint_margin = (endpoint_coverage_mm + boundary_resolution_mm * 0.5) / max(candidate.length, 1e-9)
        if (
            candidate.filament != segment.filament
            and _aligned(segment, candidate, 0.45)
            and -endpoint_margin <= projection <= 1.0 + endpoint_margin
        ):
            clipped = max(0.0, min(1.0, projection))
            qx = candidate.x1 + clipped * candidate.dx
            qy = candidate.y1 + clipped * candidate.dy
            if (px - qx) ** 2 + (py - qy) ** 2 <= transition_distance_mm ** 2:
                return True
    return False


def _reference_plan_segment(segment, inner_grid, reference_grid, *, params):
    if segment.interval_samples is None:
        segment.interval_samples = _sample_points(segment, params["boundary_resolution_mm"])
    original = segment.interval_samples
    selected = [
        _reference_point_has_opposite(
            ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5),
            segment,
            reference_grid,
            transition_distance_mm=params["transition_distance_mm"],
            boundary_resolution_mm=params["boundary_resolution_mm"],
            endpoint_coverage_mm=params["endpoint_coverage_mm"],
            cell_mm=params["cell_mm"],
        )
        for a, b in zip(original, original[1:])
    ]
    if segment.interval_cumulative is None:
        cumulative = [0.0]
        for first, second in zip(original, original[1:]):
            cumulative.append(cumulative[-1] + _distance(first, second))
        segment.interval_cumulative = cumulative
    cumulative = segment.interval_cumulative
    factors = [0.0] * len(original)
    index = 0
    while index < len(selected):
        if not selected[index]:
            index += 1
            continue
        end = index + 1
        while end < len(selected) and selected[end]:
            end += 1
        span_points = original[index : end + 1]
        if _polyline_length(span_points) >= params["minimum_span_mm"]:
            core_start, core_end = cumulative[index], cumulative[end]
            for point_index, distance in enumerate(cumulative):
                if distance < core_start:
                    factor = 1.0 - (core_start - distance) / max(params["transition_distance_mm"], 1e-9)
                elif distance > core_end:
                    factor = 1.0 - (distance - core_end) / max(params["transition_distance_mm"], 1e-9)
                else:
                    factor = 1.0
                factors[point_index] = max(factors[point_index], max(0.0, min(1.0, factor)))
        index = end
    active = [factor > 1e-9 for factor in factors]
    index = 0
    spans = []
    while index < len(active):
        if not active[index]:
            index += 1
            continue
        end = index + 1
        while end < len(active) and active[end]:
            end += 1
        first = max(0, index - 1)
        last = min(len(original) - 1, end)
        original_points = original[first : last + 1]
        shifted_points = []
        valid = True
        for point_index in range(first, last + 1):
            point = original[point_index]
            factor = factors[point_index]
            if factor <= 1e-9:
                shifted_points.append(point)
                continue
            if point_index not in segment.interval_shifted:
                segment.interval_shifted[point_index] = _reference_shift_point(
                    point,
                    segment,
                    inner_grid,
                    inset_mm=params["inset_mm"],
                    inner_search_mm=params["inner_search_mm"],
                    cell_mm=params["cell_mm"],
                )
            fully_shifted = segment.interval_shifted[point_index]
            if fully_shifted is None:
                valid = False
                break
            shifted_points.append(
                (
                    point[0] + (fully_shifted[0] - point[0]) * factor,
                    point[1] + (fully_shifted[1] - point[1]) * factor,
                )
            )
        if valid:
            spans.append((original_points, shifted_points, False, False))
        index = end
    return original, spans


DEFAULT_PARAMS = dict(
    inset_mm=0.24,
    boundary_resolution_mm=0.10,
    transition_distance_mm=0.55,
    endpoint_coverage_mm=0.21,
    inner_search_mm=1.2,
    minimum_span_mm=0.10,
    cell_mm=0.55,
)


def _make_segment(x1, y1, x2, y2, filament=0, object_id=1, line_number=1, command=b"G1", extrusion=0.1):
    return Segment(x1, y1, x2, y2, filament, object_id, line_number, command, extrusion)


def _as_tuple(segment: Segment):
    return (
        segment.x1, segment.y1, segment.x2, segment.y2, segment.filament, segment.object_id,
        segment.line_number, segment.command, segment.extrusion, segment.i, segment.j, segment.feed,
    )


def _assert_close(test_case, a, b, path=""):
    """Recursively compares nested tuples/lists of floats/bools, tolerating
    ~1-ULP differences. `sin`/`cos`/`atan2` (used for G2/G3 arc sampling) are
    NOT required by IEEE 754 to be bit-identical across math libraries, the
    way +/-/*//÷/sqrt are -- so a straight/non-arc segment's plan must match
    bit-for-bit, but an arc-derived one can differ in the last bit or two.
    1e-9mm is still ~6 orders of magnitude tighter than anything physically
    meaningful for FDM printing.
    """
    if isinstance(a, float) and isinstance(b, float):
        if a != b:
            test_case.assertAlmostEqual(a, b, delta=1e-9, msg=f"at {path}: {a!r} != {b!r}")
        return
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        test_case.assertEqual(len(a), len(b), msg=f"length mismatch at {path}")
        for index, (x, y) in enumerate(zip(a, b)):
            _assert_close(test_case, x, y, path=f"{path}[{index}]")
        return
    test_case.assertEqual(a, b, msg=f"at {path}")


class PlanSegmentParityTests(unittest.TestCase):
    def assertSamePlan(self, subject, inner_segments, reference_segments, params=None):
        params = params or DEFAULT_PARAMS
        inner_grid = _grid(inner_segments, params["cell_mm"])
        reference_grid = _grid(reference_segments, params["cell_mm"])
        python_original, python_spans = _reference_plan_segment(subject, inner_grid, reference_grid, params=params)

        rust_original, rust_spans = fuzzypaint_engine.plan_segment_once(
            _as_tuple(subject),
            [_as_tuple(s) for s in inner_segments],
            [_as_tuple(s) for s in reference_segments],
            **params,
        )
        _assert_close(self, python_original, rust_original, path="original")
        _assert_close(self, python_spans, rust_spans, path="spans")

    def test_detour_where_opposite_colour_is_nearby(self):
        subject = _make_segment(0.0, 0.0, 5.0, 0.0, filament=0, line_number=10)
        inner_segments = [_make_segment(0.0, 0.3, 5.0, 0.3, filament=0, line_number=11)]
        reference_segments = [_make_segment(1.0, -0.2, 4.0, -0.2, filament=1, line_number=20)]
        self.assertSamePlan(subject, inner_segments, reference_segments)

    def test_no_opposite_colour_yields_no_spans(self):
        subject = _make_segment(0.0, 0.0, 5.0, 0.0, filament=0, line_number=10)
        inner_segments = [_make_segment(0.0, 0.3, 5.0, 0.3, filament=0, line_number=11)]
        self.assertSamePlan(subject, inner_segments, [])

    def test_short_span_below_minimum_is_dropped(self):
        subject = _make_segment(0.0, 0.0, 5.0, 0.0, filament=0, line_number=10)
        inner_segments = [_make_segment(0.0, 0.3, 5.0, 0.3, filament=0, line_number=11)]
        reference_segments = [_make_segment(2.4, -0.2, 2.45, -0.2, filament=1, line_number=20)]
        self.assertSamePlan(
            subject, inner_segments, reference_segments,
            params={**DEFAULT_PARAMS, "minimum_span_mm": 1.0},
        )

    def test_no_inner_wall_to_shift_toward_invalidates_span(self):
        subject = _make_segment(0.0, 0.0, 5.0, 0.0, filament=0, line_number=10)
        reference_segments = [_make_segment(1.0, -0.2, 4.0, -0.2, filament=1, line_number=20)]
        self.assertSamePlan(subject, [], reference_segments)

    def test_arc_segment(self):
        subject = Segment(10.0, 0.0, 0.0, 10.0, 0, 1, 10, b"G2", 0.2, -10.0, 0.0)
        inner_segments = [_make_segment(9.0, 0.5, 0.5, 9.0, filament=0, line_number=11)]
        reference_segments = [_make_segment(8.0, -0.5, 2.0, 5.0, filament=1, line_number=20)]
        self.assertSamePlan(subject, inner_segments, reference_segments)

    def test_random_scene_matches_exactly(self):
        random.seed(20261111)
        for trial in range(15):
            subject = _make_segment(0.0, 0.0, random.uniform(2.0, 8.0), 0.0, filament=0, line_number=10)
            inner_segments = [
                _make_segment(
                    random.uniform(-1, 1), random.uniform(0.1, 0.5),
                    random.uniform(5, 9), random.uniform(0.1, 0.5),
                    filament=0, line_number=100 + i,
                )
                for i in range(5)
            ]
            reference_segments = [
                _make_segment(
                    random.uniform(0, 3), random.uniform(-0.5, -0.1),
                    random.uniform(3, 9), random.uniform(-0.5, -0.1),
                    filament=1, line_number=200 + i,
                )
                for i in range(5)
            ]
            with self.subTest(trial=trial):
                self.assertSamePlan(subject, inner_segments, reference_segments)


if __name__ == "__main__":
    unittest.main()
