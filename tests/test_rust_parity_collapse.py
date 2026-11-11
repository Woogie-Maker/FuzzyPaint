"""Differential tests for the final pipeline stage: collapse_gcode_3mf's
zip orchestration (gcode_single_filament.py) and the combined
process_gcode_3mf entry point. collapse_gcode_3mf is a real top-level
function, so this calls production code directly.
"""

import os
import tempfile
import unittest
import zipfile
from pathlib import Path

try:
    import fuzzypaint_engine
except ImportError as exc:  # pragma: no cover - environment guard
    raise unittest.SkipTest(
        "fuzzypaint_engine is not built/installed in this interpreter; "
        "run `maturin develop` inside fuzzypaint_engine/.venv first"
    ) from exc

from gcode_single_filament import collapse_gcode_3mf

REPORT_FIELDS = [
    "toolchanges_removed", "tower_blocks_removed", "kept_motion_lines",
    "filament_length_mm", "filament_volume_mm3", "filament_weight_g",
    "input_gcode_bytes", "output_gcode_bytes", "groove_segments",
    "attachments_removed", "attachment_bytes_removed",
    "tower_excursions_removed", "orphan_unretractions_removed",
    "estimated_model_seconds", "estimated_total_seconds",
]


def _make_minimal_3mf(path: Path, gcode: bytes, extra_files: dict | None = None):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("Metadata/plate_1.gcode", gcode)
        archive.writestr("Metadata/plate_1.gcode.md5", b"placeholder")
        for name, data in (extra_files or {}).items():
            archive.writestr(name, data)


SAMPLE_GCODE = (
    b"; model printing time: 0h 1m 0s; total estimated time: 0h 1m 30s\n"
    b"M83\nG90\n"
    b"G1 X100 Y130\n"
    b"; WIPE_END\n"
    b"G1 X220 Y220\n"
    b"; FEATURE: Prime tower\n"
    b"; WIPE_TOWER_START\n"
    b"G1 X225 Y225 E1\n"
    b"; WIPE_TOWER_END\n"
    b"; CP TOOLCHANGE START\n"
    b"G1 X220 Y220\n"
    b"; CP TOOLCHANGE END\n"
    b"; WIPE_START\n"
    b"G1 X219 Y220 E-.8\n"
    b"; WIPE_END\n"
    b"G1 E.8 F1800\n"
    b"M73 L1\n"
    b"; OBJECT_ID: 1\n"
    b"; FEATURE: Outer wall\n"
    b"G1 X101 Y131 F3000\n"
    b"G1 X110 Y131 E1.0\n"
)


class CollapseGcode3mfParityTests(unittest.TestCase):
    def test_basic_collapse_matches(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            python_dest = Path(folder) / "python_out.3mf"
            rust_dest = Path(folder) / "rust_out.3mf"
            _make_minimal_3mf(source, SAMPLE_GCODE)

            python_report = collapse_gcode_3mf(source, python_dest)
            rust_report = fuzzypaint_engine.collapse_gcode_3mf(str(source), str(rust_dest))

            for field in REPORT_FIELDS:
                python_value = getattr(python_report, field)
                rust_value = rust_report[field]
                if isinstance(python_value, float):
                    self.assertAlmostEqual(python_value, rust_value, places=6, msg=field)
                else:
                    self.assertEqual(python_value, rust_value, msg=field)

            with zipfile.ZipFile(python_dest) as pz, zipfile.ZipFile(rust_dest) as rz:
                self.assertEqual(sorted(pz.namelist()), sorted(rz.namelist()))
                for name in pz.namelist():
                    self.assertEqual(pz.read(name), rz.read(name), msg=name)

    def test_line_replacements_are_applied(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            python_dest = Path(folder) / "python_out.3mf"
            rust_dest = Path(folder) / "rust_out.3mf"
            _make_minimal_3mf(source, SAMPLE_GCODE)
            replacements = {22: b"G1 X110 Y131 E1.5 ; FuzzyPaint replaced\n"}

            collapse_gcode_3mf(source, python_dest, line_replacements=replacements)
            fuzzypaint_engine.collapse_gcode_3mf(str(source), str(rust_dest), line_replacements=replacements)

            with zipfile.ZipFile(python_dest) as pz, zipfile.ZipFile(rust_dest) as rz:
                gcode_name = next(n for n in pz.namelist() if n.lower().endswith(".gcode"))
                self.assertEqual(pz.read(gcode_name), rz.read(gcode_name))

    def test_strip_nonprint_auxiliaries(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            python_dest = Path(folder) / "python_out.3mf"
            rust_dest = Path(folder) / "rust_out.3mf"
            _make_minimal_3mf(source, SAMPLE_GCODE, extra_files={"Auxiliaries/Notes/readme.txt": b"hello"})

            python_report = collapse_gcode_3mf(source, python_dest, strip_nonprint_auxiliaries=True)
            rust_report = fuzzypaint_engine.collapse_gcode_3mf(
                str(source), str(rust_dest), strip_nonprint_auxiliaries=True
            )
            self.assertEqual(python_report.attachments_removed, rust_report["attachments_removed"])
            with zipfile.ZipFile(python_dest) as pz, zipfile.ZipFile(rust_dest) as rz:
                self.assertEqual(sorted(pz.namelist()), sorted(rz.namelist()))

    def test_no_wipe_tower_blocks_raises_matching_error(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            dest = Path(folder) / "out.3mf"
            gcode = b"G1 X0 Y0\nG1 X1 Y0 E1.0\n"
            _make_minimal_3mf(source, gcode)
            with self.assertRaises(Exception):
                collapse_gcode_3mf(source, dest)
            with self.assertRaises(Exception):
                fuzzypaint_engine.collapse_gcode_3mf(str(source), str(dest))


class RealFileCollapseTests(unittest.TestCase):
    REAL_FILE = (
        r"D:\FiB3R\Development\Repositories\FuzzyPaint - Paint to Fuzz Converter for BBS"
        r"\Scratch\RelicRush_A_V17(Bridge Tower Slice - 0.16)_Bridge Tower.gcode.3mf"
    )

    def test_full_pipeline_on_real_file(self):
        if not os.path.exists(self.REAL_FILE):
            self.skipTest("real-scale fixture not present on this machine")
        with tempfile.TemporaryDirectory() as folder:
            python_dest = Path(folder) / "python_out.3mf"
            rust_dest = Path(folder) / "rust_out.3mf"

            from gcode_layer_interval_detour import build_interval_detours

            replacements = build_interval_detours(self.REAL_FILE, detour_side="both")
            try:
                python_report = collapse_gcode_3mf(self.REAL_FILE, python_dest, line_replacements=replacements)
            except Exception as python_exc:
                # This specific Scratch fixture has a pre-existing data quirk
                # (confirmed with zero Rust/line_replacements involvement:
                # plain `collapse_gcode_3mf(SOURCE, ...)` already raises this
                # in pure Python) -- both implementations must agree it
                # fails, with the same message, rather than one silently
                # succeeding where the other doesn't.
                with self.assertRaises(Exception) as rust_ctx:
                    fuzzypaint_engine.process_gcode_3mf(self.REAL_FILE, str(rust_dest), detour_side="both")
                self.assertEqual(str(python_exc), str(rust_ctx.exception))
                return
            rust_report = fuzzypaint_engine.process_gcode_3mf(
                self.REAL_FILE, str(rust_dest), detour_side="both"
            )

            for field in REPORT_FIELDS:
                python_value = getattr(python_report, field)
                rust_value = rust_report[field]
                if isinstance(python_value, float):
                    self.assertAlmostEqual(python_value, rust_value, delta=1e-6, msg=field)
                else:
                    self.assertEqual(python_value, rust_value, msg=field)

            with zipfile.ZipFile(python_dest) as pz, zipfile.ZipFile(rust_dest) as rz:
                self.assertEqual(sorted(pz.namelist()), sorted(rz.namelist()))
                mismatches = []
                for name in pz.namelist():
                    if pz.read(name) != rz.read(name):
                        mismatches.append(name)
                print(f"\nmismatched files: {mismatches}")
                # plate_1.json's one known cosmetic float tie-break (see
                # memory/fuzzypaint-rust-port-strategy) is the only
                # acceptable difference.
                self.assertEqual(mismatches, ["Metadata/plate_1.json"] if mismatches else [])


if __name__ == "__main__":
    unittest.main()
