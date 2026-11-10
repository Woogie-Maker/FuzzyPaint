"""Inset outer walls at vertical virtual-colour transitions in sliced Bambu G-code."""

from __future__ import annotations

import math
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from fuzzypaint import ConversionError
from gcode_single_filament import collapse_gcode_3mf


FIELD_RE = re.compile(rb"(?:^|\s)([XYE])(-?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)")
T_RE = re.compile(rb"^T([01])$")
OBJECT_RE = re.compile(rb"^; OBJECT_ID:\s*(\d+)")
FEATURE_RE = re.compile(rb"^; FEATURE:\s*(.*)")


@dataclass(frozen=True)
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    filament: int
    object_id: int
    line_number: int
    command: bytes

    @property
    def midpoint(self):
        return ((self.x1 + self.x2) * 0.5, (self.y1 + self.y2) * 0.5)


def _closest_point(px: float, py: float, segment: Segment) -> tuple[float, float, float]:
    dx, dy = segment.x2 - segment.x1, segment.y2 - segment.y1
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return segment.x1, segment.y1, math.hypot(px - segment.x1, py - segment.y1)
    t = max(0.0, min(1.0, ((px - segment.x1) * dx + (py - segment.y1) * dy) / length2))
    qx, qy = segment.x1 + t * dx, segment.y1 + t * dy
    return qx, qy, math.hypot(px - qx, py - qy)


def _aligned(first: Segment, second: Segment, minimum: float = 0.35) -> bool:
    ax, ay = first.x2 - first.x1, first.y2 - first.y1
    bx, by = second.x2 - second.x1, second.y2 - second.y1
    al, bl = math.hypot(ax, ay), math.hypot(bx, by)
    return al > 1e-9 and bl > 1e-9 and abs((ax * bx + ay * by) / (al * bl)) >= minimum


def _grid(segments: list[Segment], cell: float):
    result = defaultdict(list)
    for segment in segments:
        mx, my = segment.midpoint
        result[(segment.object_id, math.floor(mx / cell), math.floor(my / cell))].append(segment)
    return result


def find_cross_layer_insets(
    source: Path,
    *,
    inset_mm: float = 0.12,
    transition_distance_mm: float = 0.55,
    inner_search_mm: float = 1.2,
    cell_mm: float = 0.55,
) -> dict[int, tuple[float, float]]:
    overrides: dict[int, tuple[float, float]] = {}
    lower_outer_grid = {}
    outer: list[Segment] = []
    inner: list[Segment] = []
    layer = -1
    filament = 0
    object_id = None
    feature = b""
    x = y = 0.0
    relative_xy = False
    in_cp = in_tower = False

    def nearby(grid, segment: Segment, radius_mm: float):
        mx, my = segment.midpoint
        cx, cy = math.floor(mx / cell_mm), math.floor(my / cell_mm)
        radius = math.ceil(radius_mm / cell_mm) + 1
        for gx in range(cx - radius, cx + radius + 1):
            for gy in range(cy - radius, cy + radius + 1):
                yield from grid.get((segment.object_id, gx, gy), ())

    def finalize_layer() -> None:
        nonlocal lower_outer_grid, outer, inner
        if not outer:
            lower_outer_grid = {}
            inner = []
            return
        inner_grid = _grid(inner, cell_mm)
        for segment in outer:
            # Moving an arc endpoint without recomputing its I/J centre can
            # violate firmware radius tolerances.  Preserve all G2/G3 moves.
            if segment.command != b"G1":
                continue
            mx, my = segment.midpoint
            transition = any(
                candidate.filament != segment.filament
                and _aligned(segment, candidate, 0.45)
                and _closest_point(mx, my, candidate)[2] <= transition_distance_mm
                for candidate in nearby(lower_outer_grid, segment, transition_distance_mm)
            )
            if not transition:
                continue
            best = None
            best_distance = inner_search_mm
            # Prefer an inner wall generated for the same logical material.
            candidates = list(nearby(inner_grid, segment, inner_search_mm))
            for same_only in (True, False):
                for candidate in candidates:
                    if same_only and candidate.filament != segment.filament:
                        continue
                    if not _aligned(segment, candidate):
                        continue
                    qx, qy, distance = _closest_point(segment.x2, segment.y2, candidate)
                    if 0.05 < distance < best_distance:
                        best, best_distance = (qx, qy), distance
                if best is not None:
                    break
            if best is None:
                continue
            qx, qy = best
            scale = min(inset_mm, best_distance * 0.45) / best_distance
            overrides[segment.line_number] = (
                segment.x2 + (qx - segment.x2) * scale,
                segment.y2 + (qy - segment.y2) * scale,
            )
        lower_outer_grid = _grid(outer, cell_mm)
        outer, inner = [], []

    with zipfile.ZipFile(source) as archive:
        gcode_name = next((name for name in archive.namelist() if name.lower().endswith(".gcode")), None)
        if not gcode_name:
            raise ConversionError("The sliced package contains no G-code")
        with archive.open(gcode_name) as gcode:
            for line_number, line in enumerate(gcode, 1):
                stripped = line.strip()
                if stripped == b"; CHANGE_LAYER":
                    finalize_layer()
                    layer += 1
                    feature = b""
                    continue
                tool = T_RE.match(stripped)
                if tool:
                    filament = int(tool.group(1))
                    continue
                if stripped == b"; CP TOOLCHANGE START":
                    in_cp = True
                    continue
                if stripped == b"; CP TOOLCHANGE END":
                    in_cp = False
                    continue
                if stripped == b"; WIPE_TOWER_START":
                    in_tower = True
                    continue
                if stripped == b"; WIPE_TOWER_END":
                    in_tower = False
                    continue
                obj_match = OBJECT_RE.match(stripped)
                if obj_match:
                    object_id = int(obj_match.group(1))
                    continue
                feature_match = FEATURE_RE.match(stripped)
                if feature_match:
                    feature = feature_match.group(1)
                    continue
                if stripped == b"G90":
                    relative_xy = False
                    continue
                if stripped == b"G91":
                    relative_xy = True
                    continue
                if not re.match(rb"^G[0123](?:\s|$)", stripped, re.I):
                    continue
                fields = {key: float(value) for key, value in FIELD_RE.findall(stripped)}
                nx = x + fields.get(b"X", 0.0) if relative_xy else fields.get(b"X", x)
                ny = y + fields.get(b"Y", 0.0) if relative_xy else fields.get(b"Y", y)
                extrusion = fields.get(b"E", 0.0)
                if (
                    layer >= 0 and not in_cp and not in_tower and object_id is not None
                    and extrusion > 0 and math.hypot(nx - x, ny - y) > 1e-5
                    and feature in (b"Outer wall", b"Inner wall")
                ):
                    command = stripped.split(None, 1)[0].upper()
                    segment = Segment(x, y, nx, ny, filament, object_id, line_number, command)
                    (outer if feature == b"Outer wall" else inner).append(segment)
                x, y = nx, ny
            finalize_layer()
    return overrides


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--inset", type=float, default=0.12)
    args = parser.parse_args()
    changes = find_cross_layer_insets(args.input, inset_mm=args.inset)
    print(f"Found {len(changes):,} safely insettable cross-layer segment(s)")
    report = collapse_gcode_3mf(args.input, args.output, coordinate_overrides=changes)
    print(
        f"Inset {report.groove_segments:,} segment(s); removed "
        f"{report.toolchanges_removed} tool changes and {report.tower_blocks_removed} tower blocks"
    )
