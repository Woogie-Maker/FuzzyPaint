"""Build literal inward/parallel/outward wall detours at colour Z-boundaries."""

from __future__ import annotations

import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from fuzzypaint import ConversionError
from gcode_single_filament import collapse_gcode_3mf


FIELD_RE = re.compile(rb"(?:^|\s)([XYEIJF])(-?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)")
T_RE = re.compile(rb"^T([01])$")
OBJECT_RE = re.compile(rb"^; OBJECT_ID:\s*(\d+)")
FEATURE_RE = re.compile(rb"^; FEATURE:\s*(.*)")


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    filament: int
    object_id: int
    line_number: int
    command: bytes
    extrusion: float
    i: float = 0.0
    j: float = 0.0
    feed: float | None = None
    shifted: list[tuple[float, float]] = field(default_factory=list)
    run_start: bool = False
    run_end: bool = False
    dx: float = field(init=False, repr=False, compare=False)
    dy: float = field(init=False, repr=False, compare=False)
    length2: float = field(init=False, repr=False, compare=False)
    length: float = field(init=False, repr=False, compare=False)
    unit_x: float = field(init=False, repr=False, compare=False)
    unit_y: float = field(init=False, repr=False, compare=False)
    interval_samples: list[tuple[float, float]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    interval_cumulative: list[float] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    interval_shifted: dict[int, tuple[float, float] | None] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self):
        self.dx = self.x2 - self.x1
        self.dy = self.y2 - self.y1
        self.length2 = self.dx * self.dx + self.dy * self.dy
        self.length = math.sqrt(self.length2)
        if self.length > 1e-12:
            self.unit_x, self.unit_y = self.dx / self.length, self.dy / self.length
        else:
            self.unit_x = self.unit_y = 0.0

    @property
    def midpoint(self):
        return ((self.x1 + self.x2) * 0.5, (self.y1 + self.y2) * 0.5)


def _closest_point(px, py, segment: Segment):
    if segment.length2 < 1e-12:
        return segment.x1, segment.y1, math.hypot(px - segment.x1, py - segment.y1)
    t = max(
        0.0,
        min(
            1.0,
            ((px - segment.x1) * segment.dx + (py - segment.y1) * segment.dy)
            / segment.length2,
        ),
    )
    qx, qy = segment.x1 + t * segment.dx, segment.y1 + t * segment.dy
    return qx, qy, math.hypot(px - qx, py - qy)


def _aligned(a: Segment, b: Segment, minimum=0.35):
    return (
        a.length > 1e-9
        and b.length > 1e-9
        and abs(a.unit_x * b.unit_x + a.unit_y * b.unit_y) >= minimum
    )


def _sample_points(segment: Segment, spacing: float):
    if segment.command in (b"G2", b"G3"):
        return _arc_points(segment, maximum_step=spacing)
    count = max(1, math.ceil(segment.length / spacing))
    return [
        (segment.x1 + (segment.x2 - segment.x1) * index / count,
         segment.y1 + (segment.y2 - segment.y1) * index / count)
        for index in range(count + 1)
    ]


def _grid(segments, cell):
    result = defaultdict(list)
    for segment in segments:
        occupied = {
            (math.floor(x / cell), math.floor(y / cell))
            for x, y in _sample_points(segment, cell * 0.45)
        }
        for gx, gy in occupied:
            result[(segment.object_id, gx, gy)].append(segment)
    return result


def _nearby_point(grid, object_id, x, y, radius_mm, cell_mm):
    cx, cy = math.floor(x / cell_mm), math.floor(y / cell_mm)
    radius = math.ceil(radius_mm / cell_mm) + 1
    seen = set()
    for gx in range(cx - radius, cx + radius + 1):
        for gy in range(cy - radius, cy + radius + 1):
            for segment in grid.get((object_id, gx, gy), ()):
                if segment.line_number not in seen:
                    seen.add(segment.line_number)
                    yield segment


def _arc_points(segment: Segment, maximum_step=0.35):
    cx, cy = segment.x1 + segment.i, segment.y1 + segment.j
    radius = math.hypot(segment.x1 - cx, segment.y1 - cy)
    if radius < 1e-6:
        return [(segment.x1, segment.y1), (segment.x2, segment.y2)]
    start = math.atan2(segment.y1 - cy, segment.x1 - cx)
    end = math.atan2(segment.y2 - cy, segment.x2 - cx)
    if segment.command == b"G2":
        while end >= start:
            end -= 2 * math.pi
    else:
        while end <= start:
            end += 2 * math.pi
    sweep = end - start
    count = max(1, math.ceil(abs(sweep) * radius / maximum_step))
    points = [
        (cx + radius * math.cos(start + sweep * index / count),
         cy + radius * math.sin(start + sweep * index / count))
        for index in range(count + 1)
    ]
    points[0], points[-1] = (segment.x1, segment.y1), (segment.x2, segment.y2)
    return points


def _polyline_length(points):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:]))


def build_detours(
    source: Path,
    *,
    inset_mm=0.24,
    transition_distance_mm=0.55,
    inner_search_mm=1.2,
    minimum_run_mm=0.5,
    prefer_lower_below_mm=3.0,
    boundary_resolution_mm=0.10,
    cell_mm=0.55,
):
    replacements: dict[int, bytes] = {}
    lower_outer_grid = {}
    lower_inner_grid = {}
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
        best = None
        best_distance = inner_search_mm
        candidates = list(_nearby_point(
            inner_grid, segment.object_id, px, py, inner_search_mm, cell_mm
        ))
        for same_only in (True, False):
            for candidate in candidates:
                if same_only and candidate.filament != segment.filament:
                    continue
                if not _aligned(segment, candidate):
                    continue
                qx, qy, distance = _closest_point(px, py, candidate)
                if 0.05 < distance < best_distance:
                    best, best_distance = (qx, qy), distance
            if best is not None:
                break
        if best is None:
            return None
        amount = min(inset_mm, best_distance * 0.72)
        qx, qy = best
        return (px + (qx - px) * amount / best_distance, py + (qy - py) * amount / best_distance)

    def prepare_shift(segment, inner_grid):
        original = _arc_points(segment) if segment.command in (b"G2", b"G3") else [
            (segment.x1, segment.y1), (segment.x2, segment.y2)
        ]
        shifted = [shift_point(point, segment, inner_grid) for point in original]
        if any(point is None for point in shifted):
            return False
        segment.shifted = shifted  # type: ignore[assignment]
        return True

    def group_runs(segments):
        runs = []
        current = []
        for segment in sorted(segments, key=lambda item: item.line_number):
            connected = (
                current
                and current[-1].object_id == segment.object_id
                and current[-1].filament == segment.filament
                and math.hypot(current[-1].x2 - segment.x1, current[-1].y2 - segment.y1) < 0.03
                and segment.line_number - current[-1].line_number < 12
            )
            if not connected and current:
                runs.append(current)
                current = []
            current.append(segment)
        if current:
            runs.append(current)
        return runs

    def run_length(run):
        return sum(
            _polyline_length(
                _arc_points(segment) if segment.command in (b"G2", b"G3")
                else [(segment.x1, segment.y1), (segment.x2, segment.y2)]
            )
            for segment in run
        )

    def emit_run(run, chosen_side):
        if not run or any(segment.line_number in replacements for segment in run):
            return False
        run[0].run_start, run[-1].run_end = True, True
        # Make the inset polyline continuous at shared endpoints.
        for previous, following in zip(run, run[1:]):
            join = ((previous.shifted[-1][0] + following.shifted[0][0]) * 0.5,
                    (previous.shifted[-1][1] + following.shifted[0][1]) * 0.5)
            previous.shifted[-1] = following.shifted[0] = join

        for segment in run:
            original_points = _arc_points(segment) if segment.command in (b"G2", b"G3") else [
                (segment.x1, segment.y1), (segment.x2, segment.y2)
            ]
            original_length = max(_polyline_length(original_points), 1e-9)
            e_per_mm = segment.extrusion / original_length
            emitted = []
            if segment.run_start:
                distance = math.hypot(segment.shifted[0][0] - segment.x1, segment.shifted[0][1] - segment.y1)
                emitted.append(
                    f"G1 X{segment.shifted[0][0]:.5f} Y{segment.shifted[0][1]:.5f} E{distance * e_per_mm:.6f} ; FuzzyPaint detour in ({chosen_side})"
                )
            shifted_length = max(_polyline_length(segment.shifted), 1e-9)
            for index, point in enumerate(segment.shifted[1:], 1):
                part = math.hypot(point[0] - segment.shifted[index-1][0], point[1] - segment.shifted[index-1][1])
                e_value = segment.extrusion * part / shifted_length
                feed = f" F{segment.feed:g}" if index == 1 and segment.feed is not None else ""
                emitted.append(
                    f"G1 X{point[0]:.5f} Y{point[1]:.5f} E{e_value:.6f}{feed} ; FuzzyPaint inset span ({chosen_side})"
                )
            if segment.run_end:
                distance = math.hypot(segment.x2 - segment.shifted[-1][0], segment.y2 - segment.shifted[-1][1])
                emitted.append(
                    f"G1 X{segment.x2:.5f} Y{segment.y2:.5f} E{distance * e_per_mm:.6f} ; FuzzyPaint detour out ({chosen_side})"
                )
            replacements[segment.line_number] = ("\n".join(emitted) + "\n").encode()
        return True

    def finalize_layer():
        nonlocal lower_outer_grid, lower_inner_grid, outer, inner
        if not outer:
            lower_outer_grid, lower_inner_grid, inner = {}, {}, []
            return
        inner_grid = _grid(inner, cell_mm)
        transition_matches = {}
        for segment in outer:
            matches = []
            for px, py in _sample_points(segment, boundary_resolution_mm):
                for candidate in _nearby_point(
                    lower_outer_grid,
                    segment.object_id,
                    px,
                    py,
                    transition_distance_mm,
                    cell_mm,
                ):
                    distance = _closest_point(px, py, candidate)[2]
                    if (
                        candidate.filament != segment.filament
                        and _aligned(segment, candidate, 0.45)
                        and distance <= transition_distance_mm
                    ):
                        matches.append((candidate, distance))
            if not matches:
                continue
            transition_matches[segment.line_number] = min(matches, key=lambda item: item[1])[0]

        # A boundary needs one groove, not necessarily one on the upper layer.
        # Compare each contiguous upper run with the matching lower run and use
        # the lower path for tiny/fragmented upper islands when it is cleaner.
        for upper_run in group_runs([
            segment for segment in outer if segment.line_number in transition_matches
        ]):
            upper_length = run_length(upper_run)
            lower_candidates = {
                transition_matches[segment.line_number].line_number:
                transition_matches[segment.line_number]
                for segment in upper_run
            }
            lower_runs = group_runs(lower_candidates.values())
            lower_run = max(lower_runs, key=run_length) if lower_runs else []
            lower_length = run_length(lower_run) if lower_run else 0.0

            upper_ok = (
                upper_length >= minimum_run_mm
                and all(prepare_shift(segment, inner_grid) for segment in upper_run)
            )
            lower_ok = (
                lower_length >= minimum_run_mm
                and not any(segment.line_number in replacements for segment in lower_run)
                and all(prepare_shift(segment, lower_inner_grid) for segment in lower_run)
            )
            prefer_lower = lower_ok and (
                not upper_ok
                or lower_length > upper_length * 1.05
                or (upper_length < prefer_lower_below_mm and lower_length >= upper_length * 0.95)
            )
            if prefer_lower:
                emit_run(lower_run, "lower")
            elif upper_ok:
                emit_run(upper_run, "upper")
            elif lower_ok:
                emit_run(lower_run, "lower")

        lower_outer_grid = _grid(outer, cell_mm)
        lower_inner_grid = inner_grid
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
                if (layer >= 0 and not in_cp and not in_tower and object_id is not None
                    and extrusion > 0 and math.hypot(nx-x, ny-y) > 1e-5
                    and feature in (b"Outer wall", b"Inner wall")):
                    segment = Segment(x,y,nx,ny,filament,object_id,line_number,command,extrusion,
                                      fields.get(b"I",0.0),fields.get(b"J",0.0),fields.get(b"F"))
                    (outer if feature == b"Outer wall" else inner).append(segment)
                x,y = nx,ny
            finalize_layer()
    return replacements


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inset", type=float, default=0.24)
    parser.add_argument("--boundary-resolution", type=float, default=0.10)
    args = parser.parse_args()
    if args.boundary_resolution <= 0:
        parser.error("--boundary-resolution must be greater than zero")
    plan = build_detours(
        args.input,
        inset_mm=args.inset,
        boundary_resolution_mm=args.boundary_resolution,
    )
    print(f"Built {len(plan):,} detoured source move(s)")
    report = collapse_gcode_3mf(args.input, args.output, line_replacements=plan)
    print(f"Wrote {report.groove_segments:,} detours; removed {report.toolchanges_removed} tool changes and {report.tower_blocks_removed} tower blocks; {report.filament_weight_g:.2f} g")
