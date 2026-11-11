"""Differential tests for step 5b of the Rust port: the full
build_interval_detours orchestration (finalize_layer, merge_segment_plans,
merge_plan_sets, emit_plans, the same-layer-gap channel trim) across all
three detour_side modes.

Unlike steps 2/4's targets, build_interval_detours itself IS a real
importable top-level function -- it just takes a sliced-3mf zip path rather
than raw G-code bytes, so this test wraps each fixture's G-code in a
minimal temporary zip and calls the actual production function directly.
No duplicated reference implementation needed.
"""

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

from gcode_layer_interval_detour import build_interval_detours


def _python_build(gcode: bytes, **kwargs):
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "source.gcode.3mf"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("Metadata/plate_1.gcode", gcode)
        return build_interval_detours(source, **kwargs)


def _rust_build(gcode: bytes, **kwargs):
    return fuzzypaint_engine.build_interval_detours(gcode, **kwargs)


# A small two-colour scene: two layers, each with an outer wall running
# along y=0 for filament 0 and a parallel wall at y=-0.5 for filament 1,
# plus an inner wall near y=0.3 to shift toward. Repeated across layers so
# "both" mode's cross-layer merging has something to merge.
def _two_layer_scene():
    layer_gcode = (
        b"; OBJECT_ID: 1\n"
        b"; FEATURE: Outer wall\n"
        b"G1 X0 Y0 F3000\n"
        b"G1 X5 Y0 E0.5\n"
        b"; FEATURE: Inner wall\n"
        b"G1 X0 Y0.3\n"
        b"G1 X5 Y0.3 E0.5\n"
        b"T1\n"
        b"; FEATURE: Outer wall\n"
        b"G1 X0 Y-0.5\n"
        b"G1 X5 Y-0.5 E0.5\n"
        b"T0\n"
    )
    return b"; CHANGE_LAYER\n" + layer_gcode + b"; CHANGE_LAYER\n" + layer_gcode


class OrchestrationParityTests(unittest.TestCase):
    def assertSameReplacements(self, gcode: bytes, **kwargs):
        python_result = _python_build(gcode, **kwargs)
        rust_result = _rust_build(gcode, **kwargs)
        self.assertEqual(set(python_result.keys()), set(rust_result.keys()))
        for line_number, python_bytes in python_result.items():
            self.assertEqual(python_bytes, rust_result[line_number], msg=f"line {line_number}")

    def test_both_mode_default(self):
        self.assertSameReplacements(_two_layer_scene(), detour_side="both")

    def test_upper_mode(self):
        self.assertSameReplacements(_two_layer_scene(), detour_side="upper")

    def test_lower_mode(self):
        self.assertSameReplacements(_two_layer_scene(), detour_side="lower")

    def test_upper_mode_with_same_layer_gap(self):
        # A same-layer, different-filament junction: two walls meeting
        # end-to-end on one layer should get a trimmed channel.
        gcode = (
            b"; CHANGE_LAYER\n"
            b"; OBJECT_ID: 1\n"
            b"; FEATURE: Outer wall\n"
            b"G1 X0 Y0 F3000\n"
            b"G1 X5 Y0 E0.5\n"
            b"T1\n"
            b"G1 X5.01 Y0.01 E0.0\n"
            b"G1 X10 Y0.3 E0.5\n"
            b"T0\n"
            b"; CHANGE_LAYER\n"
            b"; FEATURE: Outer wall\n"
            b"G1 X0 Y0.2\n"
            b"G1 X5 Y0.2 E0.5\n"
        )
        self.assertSameReplacements(gcode, detour_side="upper", same_layer_gap_mm=0.08)

    def test_target_layers_filter(self):
        self.assertSameReplacements(_two_layer_scene(), detour_side="both", target_layers={0})

    def test_subject_filaments_filter(self):
        self.assertSameReplacements(_two_layer_scene(), detour_side="both", subject_filaments={0})

    def test_filament_overrides(self):
        self.assertSameReplacements(
            _two_layer_scene(), detour_side="both", filament_overrides=[(1, 50, 1)]
        )

    def test_no_detour_opportunities_yields_empty(self):
        gcode = (
            b"; CHANGE_LAYER\n"
            b"; OBJECT_ID: 1\n"
            b"; FEATURE: Outer wall\n"
            b"G1 X0 Y0\n"
            b"G1 X5 Y0 E0.5\n"
        )
        self.assertSameReplacements(gcode, detour_side="both")

    def test_three_layer_scene_both_mode(self):
        scene = _two_layer_scene() + _two_layer_scene()
        self.assertSameReplacements(scene, detour_side="both")


if __name__ == "__main__":
    unittest.main()
