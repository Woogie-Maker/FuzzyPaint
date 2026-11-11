"""Differential tests for step 2 of the Rust port: the shared wall-segment
parsing loop duplicated verbatim in `gcode_layer_detour.py` and
`gcode_layer_interval_detour.py`.

`_reference_parse_wall_segments` below is a deliberate, labelled copy of that
loop's body (minus the grid/plan machinery those two modules build on top of,
which isn't ported yet) -- not an import from production code, since there is
no standalone function to import: the loop is inlined in both modules'
`build_detours`/`build_interval_detours`. Keep this in sync with that loop
until step 5 of the port replaces it outright and this file can be deleted.
"""

import io
import math
import unittest

try:
    import fuzzypaint_engine
except ImportError as exc:  # pragma: no cover - environment guard
    raise unittest.SkipTest(
        "fuzzypaint_engine is not built/installed in this interpreter; "
        "run `maturin develop` inside fuzzypaint_engine/.venv first"
    ) from exc

from gcode_layer_detour import FEATURE_RE, FIELD_RE, OBJECT_RE, T_RE


def _reference_parse_wall_segments(gcode: bytes, filament_overrides=None):
    layers = []
    outer, inner = [], []
    layer = -1
    filament = 0
    object_id = None
    feature = b""
    x = y = 0.0
    relative_xy = False
    in_cp = in_tower = False
    filament_overrides = filament_overrides or []

    for line_number, line in enumerate(io.BytesIO(gcode), 1):
        stripped = line.strip()
        if stripped == b"; CHANGE_LAYER":
            layers.append((outer, inner))
            outer, inner = [], []
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
        match = OBJECT_RE.match(stripped)
        if match:
            object_id = int(match.group(1))
            continue
        match = FEATURE_RE.match(stripped)
        if match:
            feature = match.group(1)
            continue
        if stripped == b"G90":
            relative_xy = False
            continue
        if stripped == b"G91":
            relative_xy = True
            continue
        command = stripped.split(None, 1)[0].upper() if stripped else b""
        if command not in (b"G0", b"G1", b"G2", b"G3"):
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
            and extrusion > 0
            and math.hypot(nx - x, ny - y) > 1e-5
            and feature in (b"Outer wall", b"Inner wall")
        ):
            segment_filament = filament
            for first_line, last_line, override_filament in filament_overrides:
                if first_line <= line_number <= last_line:
                    segment_filament = override_filament
                    break
            segment = (
                x, y, nx, ny, segment_filament, object_id, line_number, command, extrusion,
                fields.get(b"I", 0.0), fields.get(b"J", 0.0), fields.get(b"F"),
            )
            (outer if feature == b"Outer wall" else inner).append(segment)
        x, y = nx, ny
    layers.append((outer, inner))
    return layers


def _rust_as_tuples(layers):
    return [
        (
            [_segment_as_tuple(s) for s in outer],
            [_segment_as_tuple(s) for s in inner],
        )
        for outer, inner in layers
    ]


def _segment_as_tuple(s):
    return (
        s.x1, s.y1, s.x2, s.y2, s.filament, s.object_id, s.line_number,
        bytes(s.command), s.extrusion, s.i, s.j, s.feed,
    )


class _ParityAssertionMixin:
    def assertSameSegments(self, gcode: bytes, filament_overrides=None):
        python_layers = _reference_parse_wall_segments(gcode, filament_overrides)
        rust_layers = _rust_as_tuples(
            fuzzypaint_engine.parse_wall_segments(gcode, filament_overrides or [])
        )
        self.assertEqual(python_layers, rust_layers)


class SegmentParsingParityTests(_ParityAssertionMixin, unittest.TestCase):
    def test_basic_outer_and_inner_wall_segments(self):
        self.assertSameSegments(
            b"""G1 X0 Y0
; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X1 Y0 E.1
G1 X1 Y1 E.1
; FEATURE: Inner wall
G1 X2 Y1 E.1
; CHANGE_LAYER
; FEATURE: Outer wall
G1 X3 Y1 E.1
"""
        )

    def test_toolchange_and_tower_blocks_are_excluded(self):
        self.assertSameSegments(
            b"""; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Outer wall
; CP TOOLCHANGE START
G1 X5 Y5 E.1
; CP TOOLCHANGE END
; WIPE_TOWER_START
G1 X6 Y6 E.1
; WIPE_TOWER_END
G1 X1 Y1 E.1
"""
        )

    def test_filament_overrides_take_priority(self):
        self.assertSameSegments(
            b"""T1
; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X1 Y1 E.1
""",
            filament_overrides=[(1, 10, 0)],
        )

    def test_relative_xy_mode(self):
        self.assertSameSegments(
            b"""; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Outer wall
G90
G1 X1 Y1 E.1
G91
G1 X1 Y1 E.1
G90
G1 X5 Y5 E.1
"""
        )

    def test_arc_commands_and_feed_are_carried(self):
        self.assertSameSegments(
            b"""; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X1 Y1 E.1 F3000
G2 X2 Y1 I.5 J0 E.1
"""
        )

    def test_zero_length_moves_are_excluded(self):
        self.assertSameSegments(
            b"""; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X1 Y1 E.1
G1 X1 Y1 E.1
G1 X2 Y2 E.1
"""
        )

    def test_non_wall_features_are_excluded(self):
        self.assertSameSegments(
            b"""; CHANGE_LAYER
; OBJECT_ID: 1
; FEATURE: Internal infill
G1 X1 Y1 E.1
; FEATURE: Outer wall
G1 X2 Y2 E.1
"""
        )


class SegmentParsingRealFileParityTests(_ParityAssertionMixin, unittest.TestCase):
    """Ad-hoc, real-scale parity check -- not a fixture file (too large,
    matches the project's existing preference for small synthetic fixtures)."""

    REAL_FILE = (
        r"D:\FiB3R\Development\Repositories\FuzzyPaint - Paint to Fuzz Converter for BBS"
        r"\Scratch\RelicRush_A_V17(Bridge Tower Slice - 0.16)_Bridge Tower.gcode.3mf"
    )

    def test_real_sliced_file_matches_exactly(self):
        import os
        import zipfile

        if not os.path.exists(self.REAL_FILE):
            self.skipTest("real-scale fixture not present on this machine")
        with zipfile.ZipFile(self.REAL_FILE) as archive:
            gcode_name = next(n for n in archive.namelist() if n.lower().endswith(".gcode"))
            gcode = archive.read(gcode_name)
        self.assertSameSegments(gcode)


if __name__ == "__main__":
    unittest.main()
