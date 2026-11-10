"""Find cross-layer virtual-colour boundaries and recess them by one wall line."""

from __future__ import annotations

import math
import re
import zipfile
from collections import defaultdict
from pathlib import Path

from fuzzypaint import ConversionError
from gcode_single_filament import collapse_gcode_3mf


FIELD_RE = re.compile(rb"(?:^|\s)([XYE])(-?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)")
T_RE = re.compile(rb"^T([01])$")
OBJECT_RE = re.compile(rb"^; OBJECT_ID:\s*(\d+)")
FEATURE_RE = re.compile(rb"^; FEATURE:\s*(.*)")


def _distance_to_segment(px, py, segment) -> float:
    x1, y1, x2, y2, *_ = segment
    dx, dy = x2 - x1, y2 - y1
    length2 = dx * dx + dy * dy
    if length2 < 1e-12:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _aligned(first, second) -> bool:
    ax, ay = first[2] - first[0], first[3] - first[1]
    bx, by = second[2] - second[0], second[3] - second[1]
    al, bl = math.hypot(ax, ay), math.hypot(bx, by)
    return al > 1e-9 and bl > 1e-9 and abs((ax * bx + ay * by) / (al * bl)) >= 0.45


def find_cross_layer_boundaries(
    source: Path, *, distance_mm: float = 0.55, cell_mm: float = 0.55
) -> set[int]:
    """Return source G-code line numbers whose outer wall changes virtual colour vs. layer below."""
    selected: set[int] = set()
    previous_grid = {}
    current_segments = []
    layer = -1
    filament = 0
    object_id = None
    feature = b""
    x = y = 0.0
    relative_xy = False
    in_cp = in_tower = False

    def build_grid(segments):
        grid = defaultdict(list)
        for segment in segments:
            x1, y1, x2, y2, _, obj = segment
            mx, my = (x1 + x2) * 0.5, (y1 + y2) * 0.5
            grid[(obj, math.floor(mx / cell_mm), math.floor(my / cell_mm))].append(segment)
        return grid

    def crosses_colour(segment) -> bool:
        x1, y1, x2, y2, current_filament, obj = segment
        mx, my = (x1 + x2) * 0.5, (y1 + y2) * 0.5
        cx, cy = math.floor(mx / cell_mm), math.floor(my / cell_mm)
        radius = math.ceil(distance_mm / cell_mm) + 1
        for gx in range(cx - radius, cx + radius + 1):
            for gy in range(cy - radius, cy + radius + 1):
                for candidate in previous_grid.get((obj, gx, gy), ()):
                    if candidate[4] == current_filament or not _aligned(segment, candidate):
                        continue
                    if _distance_to_segment(mx, my, candidate) <= distance_mm:
                        return True
        return False

    with zipfile.ZipFile(source) as archive:
        gcode_name = next(
            (name for name in archive.namelist() if name.lower().endswith(".gcode")), None
        )
        if not gcode_name:
            raise ConversionError("The sliced package contains no G-code")
        with archive.open(gcode_name) as gcode:
            for line_number, line in enumerate(gcode, 1):
                stripped = line.strip()
                if stripped == b"; CHANGE_LAYER":
                    previous_grid = build_grid(current_segments)
                    current_segments = []
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
                    layer >= 0
                    and not in_cp
                    and not in_tower
                    and object_id is not None
                    and feature == b"Outer wall"
                    and extrusion > 0
                    and math.hypot(nx - x, ny - y) > 1e-5
                ):
                    segment = (x, y, nx, ny, filament, object_id)
                    if crosses_colour(segment):
                        selected.add(line_number)
                    current_segments.append(segment)
                x, y = nx, ny
    return selected


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--distance", type=float, default=0.55)
    args = parser.parse_args()
    lines = find_cross_layer_boundaries(args.input, distance_mm=args.distance)
    print(f"Found {len(lines):,} cross-layer outer-wall segment(s)")
    report = collapse_gcode_3mf(args.input, args.output, groove_lines=lines)
    print(
        f"Wrote {report.groove_segments:,} recessed segment(s); "
        f"removed {report.toolchanges_removed} tool changes and "
        f"{report.tower_blocks_removed} tower blocks"
    )
