"""Differential tests: the Rust `fuzzypaint_engine` extension must produce
byte-identical output and matching report fields to the Python originals in
`gcode_single_filament.py`, on every fixture the Python unit tests already
use. This is step 1 of the Rust port (see memory/fuzzypaint-rust-port-strategy)
-- only `_find_tower_excursion_ranges` and `_strip_marked_blocks` (scoped to
no groove_lines/coordinate_overrides/extrusion_scales/line_replacements) are
in scope so far.

Requires the extension to be built into the active interpreter first:
    fuzzypaint_engine/.venv/Scripts/python.exe -m maturin develop
"""

import io
import unittest

try:
    import fuzzypaint_engine
except ImportError as exc:  # pragma: no cover - environment guard, not a real test failure
    raise unittest.SkipTest(
        "fuzzypaint_engine is not built/installed in this interpreter; "
        "run `maturin develop` inside fuzzypaint_engine/.venv first"
    ) from exc

from gcode_single_filament import CollapseReport, _find_tower_excursion_ranges, _strip_marked_blocks


def _python_strip(gcode: bytes, skip_ranges):
    source = io.BytesIO(gcode)
    destination = io.BytesIO()
    report = CollapseReport()
    _strip_marked_blocks(source, destination, report, skip_ranges=skip_ranges)
    return destination.getvalue(), report


def _rust_strip(gcode: bytes, skip_ranges):
    return fuzzypaint_engine.strip_marked_blocks(gcode, skip_ranges)


class TowerExcursionParityTests(unittest.TestCase):
    def assertSameRanges(self, gcode: bytes):
        python_ranges = _find_tower_excursion_ranges(io.BytesIO(gcode))
        rust_ranges = fuzzypaint_engine.find_tower_excursion_ranges(gcode)
        self.assertEqual(python_ranges, rust_ranges)

    def test_finds_unmarked_travel_wrapping_a_wipe_tower(self):
        self.assertSameRanges(
            b"""G90
G1 X100 Y130
; WIPE_END
G1 X220 Y220
; FEATURE: Prime tower
; WIPE_TOWER_START
G1 X225 Y225 E1
; WIPE_TOWER_END
; CP TOOLCHANGE START
G1 X220 Y220
; CP TOOLCHANGE END
; WIPE_START
G1 X219 Y220 E-.8
; WIPE_END
; OBJECT_ID: 1
G1 X101 Y131
"""
        )

    def test_finds_departure_when_tower_starts_inside_toolchange_block(self):
        self.assertSameRanges(
            b"""G90
; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X100 Y130 E.1
G1 X220 Y220
; FEATURE: Prime tower
; CP TOOLCHANGE START
; WIPE_TOWER_START
G1 X225 Y225 E1
; WIPE_TOWER_END
; CP TOOLCHANGE END
; OBJECT_ID: 1
G1 X101 Y131
"""
        )

    def test_no_tower_activity_yields_no_ranges(self):
        self.assertSameRanges(
            b"""G90
; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X100 Y130 E.1
G1 X101 Y131 E.1
"""
        )

    def test_relative_xy_mode(self):
        self.assertSameRanges(
            b"""G90
G1 X100 Y130
G91
G1 X10 Y10
; FEATURE: Prime tower
; WIPE_TOWER_START
G1 X5 Y5 E1
; WIPE_TOWER_END
G90
; OBJECT_ID: 1
G1 X101 Y131
"""
        )


class StripMarkedBlocksParityTests(unittest.TestCase):
    def assertSameOutput(self, gcode: bytes, skip_ranges):
        python_output, python_report = _python_strip(gcode, skip_ranges)
        rust_output, rust_report = _rust_strip(gcode, skip_ranges)
        self.assertEqual(python_output, rust_output)
        self.assertEqual(python_report.toolchanges_removed, rust_report["toolchanges_removed"])
        self.assertEqual(python_report.tower_blocks_removed, rust_report["tower_blocks_removed"])
        self.assertEqual(python_report.kept_motion_lines, rust_report["kept_motion_lines"])
        self.assertAlmostEqual(python_report.filament_length_mm, rust_report["filament_length_mm"])
        self.assertEqual(
            python_report.orphan_unretractions_removed, rust_report["orphan_unretractions_removed"]
        )

    def test_tower_trip_removal_also_removes_its_orphaned_unretraction(self):
        self.assertSameOutput(
            b"""; CP TOOLCHANGE START
G1 E-2
; CP TOOLCHANGE END
G1 X220 Y220
; WIPE_START
G1 X219 Y220 E-.76
; WIPE_END
G1 E-.04 F1800
; OBJECT_ID: 1
G1 X100 Y100 F30000
G1 Z.2
G1 E.8 F1800
; FEATURE: Outer wall
G1 X101 Y100 E.1
""",
            skip_ranges=[(4, 8)],
        )

    def test_travel_only_tower_range_keeps_following_unretraction(self):
        self.assertSameOutput(
            b"""; CP TOOLCHANGE START
G1 E-2
; CP TOOLCHANGE END
G1 X220 Y220
; OBJECT_ID: 1
G1 X100 Y100 F30000
G1 Z.2
G1 E.8 F1800
; FEATURE: Outer wall
G1 X101 Y100 E.1
""",
            skip_ranges=[(4, 4)],
        )

    def test_multiple_toolchanges_and_towers(self):
        self.assertSameOutput(
            b"""; OBJECT_ID: 1
; FEATURE: Outer wall
G1 X0 Y0 E.1
; CP TOOLCHANGE START
G1 E-2
; WIPE_TOWER_START
G1 X50 Y50 E2
; WIPE_TOWER_END
G1 E2
; CP TOOLCHANGE END
G1 X1 Y1 E.1
; CP TOOLCHANGE START
G1 E-2
; CP TOOLCHANGE END
G1 X2 Y2 E.1
""",
            skip_ranges=[],
        )

    def test_crlf_line_endings_are_preserved_byte_for_byte(self):
        self.assertSameOutput(
            b"; CP TOOLCHANGE START\r\n"
            b"G1 E-2\r\n"
            b"; CP TOOLCHANGE END\r\n"
            b"; OBJECT_ID: 1\r\n"
            b"G1 X1 Y1 E.1\r\n",
            skip_ranges=[],
        )

    def test_nested_toolchange_raises_matching_error(self):
        gcode = (
            b"; CP TOOLCHANGE START\n"
            b"; CP TOOLCHANGE START\n"
            b"; CP TOOLCHANGE END\n"
        )
        with self.assertRaises(Exception) as python_ctx:
            _python_strip(gcode, [])
        with self.assertRaises(Exception) as rust_ctx:
            _rust_strip(gcode, [])
        self.assertEqual(str(python_ctx.exception), str(rust_ctx.exception))

    def test_no_toolchange_blocks_raises_matching_error(self):
        gcode = b"; OBJECT_ID: 1\nG1 X1 Y1 E.1\n"
        with self.assertRaises(Exception) as python_ctx:
            _python_strip(gcode, [])
        with self.assertRaises(Exception) as rust_ctx:
            _rust_strip(gcode, [])
        self.assertEqual(str(python_ctx.exception), str(rust_ctx.exception))

    def test_unclosed_tower_at_end_of_file_raises_matching_error(self):
        gcode = (
            b"; CP TOOLCHANGE START\n"
            b"G1 E-2\n"
            b"; CP TOOLCHANGE END\n"
            b"; WIPE_TOWER_START\n"
            b"G1 X1 Y1 E1\n"
        )
        with self.assertRaises(Exception) as python_ctx:
            _python_strip(gcode, [])
        with self.assertRaises(Exception) as rust_ctx:
            _rust_strip(gcode, [])
        self.assertEqual(str(python_ctx.exception), str(rust_ctx.exception))


if __name__ == "__main__":
    unittest.main()
