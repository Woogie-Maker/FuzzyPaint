"""Insert clipped outer-wall detours only over exact cross-layer colour spans."""

from __future__ import annotations

import math
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from fuzzypaint import ConversionError
from gcode_layer_detour import (
    FEATURE_RE,
    FIELD_RE,
    OBJECT_RE,
    T_RE,
    Segment,
    _aligned,
    _closest_point,
    _grid,
    _nearby_point,
    _polyline_length,
    _sample_points,
)
from gcode_single_filament import collapse_gcode_3mf


@dataclass
class Span:
    original: list[tuple[float, float]]
    shifted: list[tuple[float, float]]
    continue_in: bool = False
    continue_out: bool = False


@dataclass
class SegmentPlan:
    segment: Segment
    original: list[tuple[float, float]]
    spans: list[Span] = field(default_factory=list)


def _distance(a, b):
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _same_point(a, b, tolerance=0.025):
    return _distance(a, b) <= tolerance


def _rdp_indices(points, tolerance):
    if len(points) <= 2:
        return [0, len(points) - 1] if len(points) == 2 else [0]
    ax, ay = points[0]
    bx, by = points[-1]
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    best_index, best_distance = 0, -1.0
    for index, (px, py) in enumerate(points[1:-1], 1):
        if length2 < 1e-12:
            distance = math.hypot(px - ax, py - ay)
        else:
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length2))
            distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if distance > best_distance:
            best_index, best_distance = index, distance
    if best_distance <= tolerance:
        return [0, len(points) - 1]
    left = _rdp_indices(points[:best_index + 1], tolerance)
    right = _rdp_indices(points[best_index:], tolerance)
    return left[:-1] + [best_index + index for index in right]


def _simplify_paired(original, shifted, tolerance=0.01):
    indices = sorted(set(_rdp_indices(original, tolerance)) | set(_rdp_indices(shifted, tolerance)))
    return [original[index] for index in indices], [shifted[index] for index in indices]


def _line_to(point, extrusion, comment=""):
    suffix = f" ; {comment}" if comment else ""
    return f"G1 X{point[0]:.5f} Y{point[1]:.5f} E{extrusion:.6f}{suffix}"


def _travel_to(point, comment=""):
    suffix = f" ; {comment}" if comment else ""
    return f"G0 X{point[0]:.5f} Y{point[1]:.5f}{suffix}"


def _emit_plan(plan: SegmentPlan):
    segment = plan.segment
    for span in plan.spans:
        if len(span.original) > 2:
            span.original, span.shifted = _simplify_paired(
                span.original, span.shifted, tolerance=0.01
            )
    original_length = max(_polyline_length(plan.original), 1e-9)
    e_per_mm = segment.extrusion / original_length
    emitted: list[str] = []
    cursor_index = 0
    feed_pending = segment.feed

    def emit(point, extrusion, comment=""):
        nonlocal feed_pending
        line = _line_to(point, extrusion, comment)
        if feed_pending is not None:
            line = line.replace(" ;", f" F{feed_pending:g} ;", 1) if " ;" in line else line + f" F{feed_pending:g}"
            feed_pending = None
        emitted.append(line)

    for span in plan.spans:
        start, end = span.original[0], span.original[-1]
        start_index = next(
            index for index in range(cursor_index, len(plan.original))
            if plan.original[index] == start
        )
        end_index = next(
            index for index in range(start_index, len(plan.original))
            if plan.original[index] == end
        )
        unchanged = plan.original[cursor_index:start_index + 1]
        if segment.command == b"G1" and len(unchanged) > 1:
            unchanged = [unchanged[0], unchanged[-1]]
        elif len(unchanged) > 2:
            unchanged = [unchanged[index] for index in _rdp_indices(unchanged, 0.01)]
        for previous, point in zip(unchanged, unchanged[1:]):
            emit(
                point,
                _distance(previous, point) * e_per_mm,
                "FuzzyPaint unchanged prefix",
            )
        if not span.continue_in and not _same_point(start, span.shifted[0], 1e-5):
            emitted.append(_travel_to(span.shifted[0], "FuzzyPaint tapered detour in"))
        shifted_length = max(_polyline_length(span.shifted), 1e-9)
        span_extrusion = _polyline_length(span.original) * e_per_mm
        for previous, point in zip(span.shifted, span.shifted[1:]):
            emit(
                point,
                span_extrusion * _distance(previous, point) / shifted_length,
                "FuzzyPaint clipped inset span",
            )
        if not span.continue_out and not _same_point(span.shifted[-1], end, 1e-5):
            emitted.append(_travel_to(end, "FuzzyPaint tapered detour out"))
        cursor_index = end_index
    unchanged = plan.original[cursor_index:]
    if segment.command == b"G1" and len(unchanged) > 1:
        unchanged = [unchanged[0], unchanged[-1]]
    elif len(unchanged) > 2:
        unchanged = [unchanged[index] for index in _rdp_indices(unchanged, 0.01)]
    for previous, point in zip(unchanged, unchanged[1:]):
        emit(
            point,
            _distance(previous, point) * e_per_mm,
            "FuzzyPaint unchanged suffix",
        )
    return ("\n".join(emitted) + "\n").encode()


def build_interval_detours(
    source: Path,
    *,
    inset_mm=0.24,
    boundary_resolution_mm=0.10,
    transition_distance_mm=0.55,
    endpoint_coverage_mm=0.21,
    inner_search_mm=1.2,
    minimum_span_mm=0.10,
    same_layer_gap_mm=0.08,
    same_layer_endpoint_tolerance_mm=0.65,
    detour_side="upper",
    target_layers: set[int] | None = None,
    subject_filaments: set[int] | None = None,
    filament_overrides: list[tuple[int, int, int]] | None = None,
    cell_mm=0.55,
):
    if detour_side not in {"upper", "lower", "both"}:
        raise ConversionError("detour_side must be 'upper', 'lower', or 'both'")
    replacements: dict[int, bytes] = {}
    lower_outer_grid = {}
    lower_inner_grid = {}
    lower_outer: list[Segment] = []
    lower_inner: list[Segment] = []
    pending_upper_plans: dict[int, SegmentPlan] = {}

    def eligible_subjects(segments):
        if subject_filaments is None:
            return segments
        return [segment for segment in segments if segment.filament in subject_filaments]
    outer: list[Segment] = []
    inner: list[Segment] = []
    layer = -1
    filament = 0
    object_id = None
    feature = b""
    x = y = 0.0
    relative_xy = False
    in_cp = in_tower = False

    def shift_point(point, segment, inner_grid):
        px, py = point
        same_best = any_best = None
        same_distance2 = any_distance2 = inner_search_mm * inner_search_mm
        for candidate in _nearby_point(
            inner_grid, segment.object_id, px, py, inner_search_mm, cell_mm
        ):
            if not _aligned(segment, candidate):
                continue
            if candidate.length2 < 1e-12:
                qx, qy = candidate.x1, candidate.y1
            else:
                projection = max(
                    0.0,
                    min(
                        1.0,
                        ((px - candidate.x1) * candidate.dx + (py - candidate.y1) * candidate.dy)
                        / candidate.length2,
                    ),
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
        return (px + (qx - px) * amount / best_distance,
                py + (qy - py) * amount / best_distance)

    def point_has_opposite(point, segment, reference_grid):
        px, py = point
        for candidate in _nearby_point(
            reference_grid,
            segment.object_id,
            px,
            py,
            transition_distance_mm,
            cell_mm,
        ):
            if candidate.length2 < 1e-12:
                continue
            projection = (
                (px - candidate.x1) * candidate.dx + (py - candidate.y1) * candidate.dy
            ) / candidate.length2
            # Colour occupies the swept extrusion bead, whose rounded cap
            # extends beyond the source centreline endpoint.  Include one
            # bead radius plus half a sampling interval longitudinally.
            endpoint_margin = (
                endpoint_coverage_mm + boundary_resolution_mm * 0.5
            ) / max(candidate.length, 1e-9)
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

    def plan_segment(segment, inner_grid, reference_grid):
        if segment.interval_samples is None:
            segment.interval_samples = _sample_points(segment, boundary_resolution_mm)
        original = segment.interval_samples
        selected = [
            point_has_opposite(
                ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5),
                segment,
                reference_grid,
            )
            for a, b in zip(original, original[1:])
        ]
        plan = SegmentPlan(segment=segment, original=original)
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
            span_points = original[index:end + 1]
            if _polyline_length(span_points) >= minimum_span_mm:
                core_start, core_end = cumulative[index], cumulative[end]
                for point_index, distance in enumerate(cumulative):
                    if distance < core_start:
                        factor = 1.0 - (core_start - distance) / max(transition_distance_mm, 1e-9)
                    elif distance > core_end:
                        factor = 1.0 - (distance - core_end) / max(transition_distance_mm, 1e-9)
                    else:
                        factor = 1.0
                    factors[point_index] = max(factors[point_index], max(0.0, min(1.0, factor)))
            index = end
        active = [factor > 1e-9 for factor in factors]
        index = 0
        while index < len(active):
            if not active[index]:
                index += 1
                continue
            end = index + 1
            while end < len(active) and active[end]:
                end += 1
            first = max(0, index - 1)
            last = min(len(original) - 1, end)
            original_points = original[first:last + 1]
            shifted_points = []
            valid = True
            for point_index in range(first, last + 1):
                point = original[point_index]
                factor = factors[point_index]
                if factor <= 1e-9:
                    shifted_points.append(point)
                    continue
                if point_index not in segment.interval_shifted:
                    segment.interval_shifted[point_index] = shift_point(
                        point, segment, inner_grid
                    )
                fully_shifted = segment.interval_shifted[point_index]
                if fully_shifted is None:
                    valid = False
                    break
                shifted_points.append((
                    point[0] + (fully_shifted[0] - point[0]) * factor,
                    point[1] + (fully_shifted[1] - point[1]) * factor,
                ))
            if valid:
                plan.spans.append(Span(original_points, shifted_points))
            index = end
        return plan

    def merge_segment_plans(first: SegmentPlan, second: SegmentPlan):
        if len(first.original) != len(second.original) or any(
            not _same_point(a, b, 1e-6)
            for a, b in zip(first.original, second.original)
        ):
            # Both plans came from the same source segment and resolution; this
            # is defensive only. Prefer the plan covering more original path.
            first_length = sum(_polyline_length(span.original) for span in first.spans)
            second_length = sum(_polyline_length(span.original) for span in second.spans)
            return first if first_length >= second_length else second
        point_indices = defaultdict(list)
        for index, point in enumerate(first.original):
            point_indices[point].append(index)
        selected = [False] * (len(first.original) - 1)
        shifted_points: dict[int, tuple[float, float]] = {}
        for plan in (first, second):
            for span in plan.spans:
                starts = point_indices.get(span.original[0], ())
                ends = point_indices.get(span.original[-1], ())
                if not starts or not ends:
                    continue
                start = starts[0]
                end = next((index for index in ends if index >= start), ends[-1])
                for index in range(start, end):
                    selected[index] = True
                for offset, point in enumerate(span.shifted):
                    shifted_points[start + offset] = point
        merged = SegmentPlan(segment=first.segment, original=first.original)
        index = 0
        while index < len(selected):
            if not selected[index]:
                index += 1
                continue
            end = index + 1
            while end < len(selected) and selected[end]:
                end += 1
            original_points = first.original[index:end + 1]
            shifted = [shifted_points.get(i) for i in range(index, end + 1)]
            if all(point is not None for point in shifted):
                merged.spans.append(Span(original_points, shifted))  # type: ignore[arg-type]
            index = end
        return merged

    def merge_plan_sets(*plan_sets):
        merged: dict[int, SegmentPlan] = {}
        for plans in plan_sets:
            for plan in plans:
                line_number = plan.segment.line_number
                if line_number in merged:
                    merged[line_number] = merge_segment_plans(merged[line_number], plan)
                else:
                    merged[line_number] = plan
        return list(merged.values())

    def emit_plans(plans):
        plans = sorted((plan for plan in plans if plan.spans),
                       key=lambda plan: plan.segment.line_number)
        # Keep a continuous inset across adjacent source moves instead of
        # creating an outward/inward tooth at every original G-code line.
        for previous, following in zip(plans, plans[1:]):
            if (
                previous.segment.object_id == following.segment.object_id
                and previous.segment.filament == following.segment.filament
                and following.segment.line_number - previous.segment.line_number < 12
                and _same_point((previous.segment.x2, previous.segment.y2),
                                (following.segment.x1, following.segment.y1), 0.03)
                and _same_point(previous.spans[-1].original[-1],
                                (previous.segment.x2, previous.segment.y2))
                and _same_point(following.spans[0].original[0],
                                (following.segment.x1, following.segment.y1))
            ):
                join = (
                    (previous.spans[-1].shifted[-1][0] + following.spans[0].shifted[0][0]) * 0.5,
                    (previous.spans[-1].shifted[-1][1] + following.spans[0].shifted[0][1]) * 0.5,
                )
                previous.spans[-1].shifted[-1] = join
                following.spans[0].shifted[0] = join
                previous.spans[-1].continue_out = True
                following.spans[0].continue_in = True
        for plan in plans:
            replacements[plan.segment.line_number] = _emit_plan(plan)

    def finalize_layer():
        nonlocal lower_outer_grid, lower_inner_grid, lower_outer, lower_inner, outer, inner, pending_upper_plans
        if not outer:
            lower_outer_grid, lower_inner_grid, lower_outer, lower_inner, inner = {}, {}, [], [], []
            return
        current_display_layer = layer + 1
        lower_display_layer = layer
        current_grid = _grid(outer, cell_mm)
        current_inner_grid = _grid(inner, cell_mm) if detour_side == "both" else {}
        if detour_side == "both":
            lower_should_plan = (
                bool(lower_outer)
                and (target_layers is None or lower_display_layer in target_layers)
            )
            lower_plans = []
            if lower_should_plan:
                lower_plans = [
                    plan_segment(segment, lower_inner_grid, current_grid)
                    for segment in eligible_subjects(lower_outer)
                ]
            emit_plans(merge_plan_sets(pending_upper_plans.values(), lower_plans))

            upper_should_plan = (
                bool(lower_outer_grid)
                and (target_layers is None or current_display_layer in target_layers)
            )
            current_upper_plans = []
            if upper_should_plan:
                current_upper_plans = [
                    plan_segment(segment, current_inner_grid, lower_outer_grid)
                    for segment in eligible_subjects(outer)
                ]
            pending_upper_plans = {
                plan.segment.line_number: plan
                for plan in current_upper_plans if plan.spans
            }
            plans = []
            should_plan = False
        elif detour_side == "upper":
            should_plan = target_layers is None or current_display_layer in target_layers
            subjects = eligible_subjects(outer)
            subject_inner_grid = _grid(inner, cell_mm) if should_plan else {}
            reference_grid = lower_outer_grid
        else:
            should_plan = (
                bool(lower_outer)
                and (target_layers is None or lower_display_layer in target_layers)
            )
            subjects = eligible_subjects(lower_outer)
            subject_inner_grid = _grid(lower_inner, cell_mm) if should_plan else {}
            reference_grid = _grid(outer, cell_mm) if should_plan else {}
        plans = (
            [plan_segment(segment, subject_inner_grid, reference_grid) for segment in subjects]
            if detour_side != "both" and should_plan and reference_grid else []
        )
        plans = [plan for plan in plans if plan.spans]
        if detour_side != "both":
            emit_plans(plans)

        # Where two virtual colours meet end-to-end on the same layer, pull
        # each side back by half the requested channel width. This supplies
        # the left/right edge that a cross-layer detour cannot create.
        if same_layer_gap_mm > 0 and detour_side == "upper" and should_plan:
            endpoint_cell = max(same_layer_endpoint_tolerance_mm * 2.0, 0.02)
            endpoint_grid = defaultdict(list)
            endpoints = []
            for segment in outer:
                if segment.command != b"G1":
                    continue
                length = math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1)
                if length <= same_layer_gap_mm * 1.25:
                    continue
                for at_start, px, py in (
                    (True, segment.x1, segment.y1),
                    (False, segment.x2, segment.y2),
                ):
                    record = (segment, at_start, px, py)
                    endpoints.append(record)
                    endpoint_grid[(segment.object_id, math.floor(px / endpoint_cell),
                                   math.floor(py / endpoint_cell))].append(record)
            trims = defaultdict(lambda: [False, False])
            for segment, at_start, px, py in endpoints:
                cx, cy = math.floor(px / endpoint_cell), math.floor(py / endpoint_cell)
                found = False
                for gx in range(cx - 1, cx + 2):
                    for gy in range(cy - 1, cy + 2):
                        for other, other_start, qx, qy in endpoint_grid.get(
                            (segment.object_id, gx, gy), ()
                        ):
                            if (
                                other.line_number != segment.line_number
                                and other.filament != segment.filament
                                and _aligned(segment, other, 0.75)
                            ):
                                junction_distance = math.hypot(px - qx, py - qy)
                                if not 0.20 <= junction_distance <= same_layer_endpoint_tolerance_mm:
                                    continue
                                vx, vy = (qx - px) / junction_distance, (qy - py) / junction_distance
                                segment_length = math.hypot(segment.x2 - segment.x1,
                                                            segment.y2 - segment.y1)
                                other_length = math.hypot(other.x2 - other.x1,
                                                          other.y2 - other.y1)
                                if at_start:
                                    sx, sy = ((segment.x1 - segment.x2) / segment_length,
                                              (segment.y1 - segment.y2) / segment_length)
                                else:
                                    sx, sy = ((segment.x2 - segment.x1) / segment_length,
                                              (segment.y2 - segment.y1) / segment_length)
                                if other_start:
                                    ox, oy = ((other.x1 - other.x2) / other_length,
                                              (other.y1 - other.y2) / other_length)
                                else:
                                    ox, oy = ((other.x2 - other.x1) / other_length,
                                              (other.y2 - other.y1) / other_length)
                                # Both endpoint tangents must face into the
                                # centreline gap, excluding merely nearby walls.
                                if sx * vx + sy * vy < 0.70 or ox * -vx + oy * -vy < 0.70:
                                    continue
                                trims[segment.line_number][0 if at_start else 1] = True
                                found = True
                                break
                        if found:
                            break
                    if found:
                        break
            half_gap = same_layer_gap_mm * 0.5
            by_line = {segment.line_number: segment for segment in outer}
            for line_number, (trim_start, trim_end) in trims.items():
                if line_number in replacements:
                    continue
                segment = by_line[line_number]
                length = math.hypot(segment.x2 - segment.x1, segment.y2 - segment.y1)
                start_amount = min(half_gap, length * 0.2) if trim_start else 0.0
                end_amount = min(half_gap, length * 0.2) if trim_end else 0.0
                if start_amount + end_amount >= length * 0.8:
                    continue
                ux, uy = (segment.x2 - segment.x1) / length, (segment.y2 - segment.y1) / length
                start = (segment.x1 + ux * start_amount, segment.y1 + uy * start_amount)
                end = (segment.x2 - ux * end_amount, segment.y2 - uy * end_amount)
                lines = []
                if trim_start:
                    lines.append(
                        f"G0 X{start[0]:.5f} Y{start[1]:.5f} ; FuzzyPaint same-layer channel start"
                    )
                extrusion = segment.extrusion * (length - start_amount - end_amount) / length
                feed = f" F{segment.feed:g}" if segment.feed is not None else ""
                lines.append(
                    f"G1 X{end[0]:.5f} Y{end[1]:.5f} E{extrusion:.6f}{feed} ; FuzzyPaint same-layer channel span"
                )
                if trim_end:
                    lines.append(
                        f"G0 X{segment.x2:.5f} Y{segment.y2:.5f} ; FuzzyPaint same-layer channel end"
                    )
                replacements[line_number] = ("\n".join(lines) + "\n").encode()
        lower_outer_grid = current_grid if detour_side in {"upper", "both"} else {}
        lower_inner_grid = current_inner_grid if detour_side == "both" else {}
        lower_outer, lower_inner = outer, inner
        outer, inner = [], []

    with zipfile.ZipFile(source) as archive:
        gcode_name = next((name for name in archive.namelist() if name.lower().endswith(".gcode")), None)
        if not gcode_name:
            raise ConversionError("The sliced package contains no G-code")
        with archive.open(gcode_name) as gcode:
            for line_number, line in enumerate(gcode, 1):
                stripped = line.strip()
                if stripped == b"; CHANGE_LAYER":
                    finalize_layer(); layer += 1; feature = b""; continue
                tool = T_RE.match(stripped)
                if tool: filament = int(tool.group(1)); continue
                if stripped == b"; CP TOOLCHANGE START": in_cp = True; continue
                if stripped == b"; CP TOOLCHANGE END": in_cp = False; continue
                if stripped == b"; WIPE_TOWER_START": in_tower = True; continue
                if stripped == b"; WIPE_TOWER_END": in_tower = False; continue
                match = OBJECT_RE.match(stripped)
                if match: object_id = int(match.group(1)); continue
                match = FEATURE_RE.match(stripped)
                if match: feature = match.group(1); continue
                if stripped == b"G90": relative_xy = False; continue
                if stripped == b"G91": relative_xy = True; continue
                command = stripped.split(None, 1)[0].upper() if stripped else b""
                if command not in (b"G0", b"G1", b"G2", b"G3"): continue
                fields = {key: float(value) for key, value in FIELD_RE.findall(stripped)}
                nx = x + fields.get(b"X", 0.0) if relative_xy else fields.get(b"X", x)
                ny = y + fields.get(b"Y", 0.0) if relative_xy else fields.get(b"Y", y)
                extrusion = fields.get(b"E", 0.0)
                if (
                    layer >= 0 and not in_cp and not in_tower and object_id is not None
                    and extrusion > 0 and math.hypot(nx - x, ny - y) > 1e-5
                    and feature in (b"Outer wall", b"Inner wall")
                ):
                    segment_filament = filament
                    if filament_overrides:
                        for first_line, last_line, override_filament in filament_overrides:
                            if first_line <= line_number <= last_line:
                                segment_filament = override_filament
                                break
                    segment = Segment(
                        x, y, nx, ny, segment_filament, object_id, line_number, command, extrusion,
                        fields.get(b"I", 0.0), fields.get(b"J", 0.0), fields.get(b"F")
                    )
                    (outer if feature == b"Outer wall" else inner).append(segment)
                x, y = nx, ny
            finalize_layer()
            if detour_side == "both":
                emit_plans(pending_upper_plans.values())
    return replacements


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inset", type=float, default=0.24)
    parser.add_argument("--boundary-resolution", type=float, default=0.10)
    parser.add_argument("--same-layer-gap", type=float, default=0.08)
    parser.add_argument("--endpoint-coverage", type=float, default=0.21)
    parser.add_argument("--detour-side", choices=("upper", "lower", "both"), default="upper")
    parser.add_argument(
        "--target-layers",
        help="Comma-separated Bambu Studio display layers to modify (for diagnostics)",
    )
    parser.add_argument(
        "--detour-filaments",
        help="Comma-separated one-based virtual filament numbers eligible for detours",
    )
    parser.add_argument(
        "--filament-override",
        action="append",
        default=[],
        metavar="FIRST-LAST:FILAMENT",
        help=(
            "Treat source G-code lines FIRST through LAST as the specified one-based "
            "filament while detecting boundaries. May be supplied more than once."
        ),
    )
    args = parser.parse_args()
    target_layers = None
    if args.target_layers:
        try:
            target_layers = {int(value.strip()) for value in args.target_layers.split(",")}
        except ValueError:
            parser.error("--target-layers must contain comma-separated integers")
    subject_filaments = None
    if args.detour_filaments:
        try:
            subject_filaments = {
                int(value.strip()) - 1 for value in args.detour_filaments.split(",")
            }
        except ValueError:
            parser.error("--detour-filaments must contain comma-separated integers")
        if any(value < 0 for value in subject_filaments):
            parser.error("--detour-filaments values must be at least 1")
    filament_overrides = []
    for value in args.filament_override:
        match = __import__("re").fullmatch(r"(\d+)-(\d+):(\d+)", value.strip())
        if not match:
            parser.error("--filament-override must use FIRST-LAST:FILAMENT")
        first_line, last_line, override_filament = map(int, match.groups())
        if first_line < 1 or last_line < first_line or override_filament < 1:
            parser.error("--filament-override values must be positive and ordered")
        filament_overrides.append((first_line, last_line, override_filament - 1))
    plan = build_interval_detours(
        args.input,
        inset_mm=args.inset,
        boundary_resolution_mm=args.boundary_resolution,
        endpoint_coverage_mm=args.endpoint_coverage,
        same_layer_gap_mm=args.same_layer_gap,
        detour_side=args.detour_side,
        target_layers=target_layers,
        subject_filaments=subject_filaments,
        filament_overrides=filament_overrides,
    )
    print(f"Built {len(plan):,} clipped source move replacement(s)")
    report = collapse_gcode_3mf(args.input, args.output, line_replacements=plan)
    print(
        f"Wrote {report.groove_segments:,} clipped replacements; removed "
        f"{report.toolchanges_removed} tool changes and "
        f"{report.tower_blocks_removed} tower blocks; "
        f"{report.filament_weight_g:.2f} g"
    )
