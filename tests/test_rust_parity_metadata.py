"""Differential tests for the metadata/XML/JSON rewriting port in
gcode_single_filament.py: _parse_time_text, _format_time,
_estimate_progress_times, _rewrite_gcode_metadata, _rewrite_slice_info,
_rewrite_model_settings, _rewrite_plate_json, _rewrite_sequence,
_rewrite_project_settings.

All of these are real importable module-level functions, so this test
calls production code directly.
"""

import io
import random
import unittest

try:
    import fuzzypaint_engine
except ImportError as exc:  # pragma: no cover - environment guard
    raise unittest.SkipTest(
        "fuzzypaint_engine is not built/installed in this interpreter; "
        "run `maturin develop` inside fuzzypaint_engine/.venv first"
    ) from exc

from gcode_single_filament import (
    CollapseReport,
    _estimate_progress_times,
    _format_time,
    _parse_time_text,
    _rewrite_gcode_metadata,
    _rewrite_model_settings,
    _rewrite_plate_json,
    _rewrite_project_settings,
    _rewrite_sequence,
    _rewrite_slice_info,
)


class TimeTextParityTests(unittest.TestCase):
    def test_parse_time_text(self):
        for value in (b"1h 2m 3s", b"5m 10s", b"42s", b"", b"3h", b"59m 59s"):
            self.assertEqual(_parse_time_text(value), fuzzypaint_engine.parse_time_text(value))

    def test_format_time(self):
        for value in (0.0, 1.0, 59.0, 60.0, 61.0, 3599.0, 3600.0, 3723.5, 86399.0):
            self.assertEqual(_format_time(value), fuzzypaint_engine.format_time(value))


SAMPLE_GCODE = b"""; model printing time: 1h 2m 0s; total estimated time: 1h 5m 0s
M204 S500
G90
M83
T0
; OBJECT_ID: 1
M73 L1
M73 P0 R10
G1 X0 Y0 F3000
G1 X10 Y0 E1.0
M73 P50 R5
; CHANGE_LAYER
M73 L2
G2 X10 Y10 I0 J5 E0.5
M73 P100 R0
"""


class EstimateProgressTimesParityTests(unittest.TestCase):
    def test_matches_on_sample_gcode(self):
        python_model, python_prep, python_remaining = _estimate_progress_times(io.BytesIO(SAMPLE_GCODE))
        rust_model, rust_prep, rust_remaining = fuzzypaint_engine.estimate_progress_times(SAMPLE_GCODE)
        self.assertAlmostEqual(python_model, rust_model, places=9)
        self.assertAlmostEqual(python_prep, rust_prep, places=9)
        self.assertEqual(set(python_remaining.keys()), set(rust_remaining.keys()))
        for line, value in python_remaining.items():
            self.assertAlmostEqual(value, rust_remaining[line], places=9)

    def test_random_motion_sequences(self):
        random.seed(20261111)
        for _ in range(20):
            lines = [b"M83", b"G90", b"M73 L1"]
            for _ in range(30):
                command = random.choice([b"G0", b"G1", b"G2", b"G3"])
                x = random.uniform(-50, 50)
                y = random.uniform(-50, 50)
                e = random.uniform(0, 2)
                feed = random.choice([1800, 3000, 6000, 9000])
                if command in (b"G2", b"G3"):
                    i = random.uniform(-10, 10)
                    j = random.uniform(-10, 10)
                    lines.append(f"{command.decode()} X{x:.3f} Y{y:.3f} I{i:.3f} J{j:.3f} E{e:.4f} F{feed}".encode())
                else:
                    lines.append(f"{command.decode()} X{x:.3f} Y{y:.3f} E{e:.4f} F{feed}".encode())
            gcode = b"\n".join(lines) + b"\n"
            python_model, python_prep, _ = _estimate_progress_times(io.BytesIO(gcode))
            rust_model, rust_prep, _ = fuzzypaint_engine.estimate_progress_times(gcode)
            self.assertAlmostEqual(python_model, rust_model, places=6)
            self.assertAlmostEqual(python_prep, rust_prep, places=6)


class RewriteGcodeMetadataParityTests(unittest.TestCase):
    def test_matches_on_sample_gcode(self):
        report = CollapseReport()
        report.filament_length_mm = 1234.5
        python_destination = io.BytesIO()
        python_digest = _rewrite_gcode_metadata(io.BytesIO(SAMPLE_GCODE), python_destination, report)
        python_output = python_destination.getvalue()

        rust_output, rust_digest, rust_volume, rust_weight, rust_model_s, rust_total_s = (
            fuzzypaint_engine.rewrite_gcode_metadata(SAMPLE_GCODE, 1234.5)
        )
        self.assertEqual(python_output, rust_output)
        self.assertEqual(python_digest, rust_digest.encode("ascii"))
        self.assertAlmostEqual(report.filament_volume_mm3, rust_volume, places=6)
        self.assertAlmostEqual(report.filament_weight_g, rust_weight, places=6)
        self.assertAlmostEqual(report.estimated_model_seconds, rust_model_s, places=6)
        self.assertAlmostEqual(report.estimated_total_seconds, rust_total_s, places=6)

    def test_ams_and_tool_select_rewriting(self):
        gcode = (
            b"M620 S5A1\n"
            b"M621 S255A\n"
            b"T1\n"
            b"T255\n"
            b"T0 ; keep going\n"
            b"; filament_map = 1,2,3\n"
            b"; filament_colour = #FF0000;#00FF00;\n"
            b"M73 L1\n"
            b"M73 P10 R5\n"
            b"; CP TOOLCHANGE START\n"
            b"; CP TOOLCHANGE END\n"
        )
        report = CollapseReport()
        report.filament_length_mm = 500.0
        destination = io.BytesIO()
        python_digest = _rewrite_gcode_metadata(io.BytesIO(gcode), destination, report)
        python_output = destination.getvalue()
        rust_output, rust_digest, *_ = fuzzypaint_engine.rewrite_gcode_metadata(gcode, 500.0)
        self.assertEqual(python_output, rust_output)
        self.assertEqual(python_digest, rust_digest.encode("ascii"))


SLICE_INFO_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <header>
    <header_item key="X-BBL-Client-Type" value="slicer"/>
  </header>
  <plate>
    <metadata key="weight" value="33.95"/>
    <metadata key="prediction" value="12066"/>
    <metadata key="filament_maps" value="1 1"/>
    <metadata key="limit_filament_maps" value="0 0"/>
    <filament id="1" tray_info_idx="GFA01" type="PLA" color="#757575" used_m="6.73" used_g="21.37"/>
    <filament id="2" tray_info_idx="GFA01" type="PLA" color="#68724D" used_m="3.96" used_g="12.58"/>
    <layer_filament_lists>
      <layer_filament_list filament_list="0 1" layer_ranges="0 74"/>
    </layer_filament_lists>
  </plate>
</config>
"""

MODEL_SETTINGS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<config>
  <plate>
    <metadata key="plater_id" value="1"/>
    <metadata key="filament_maps" value="1 1"/>
    <metadata key="filament_volume_maps" value="0 0"/>
  </plate>
</config>
"""


class XmlRewriteParityTests(unittest.TestCase):
    def test_rewrite_slice_info(self):
        report = CollapseReport()
        report.filament_weight_g = 25.5
        report.filament_length_mm = 8000.0
        report.estimated_total_seconds = 650.4
        python_result = _rewrite_slice_info(SLICE_INFO_XML, report)
        rust_result = fuzzypaint_engine.rewrite_slice_info(SLICE_INFO_XML, 25.5, 8000.0, 650.4)
        self.assertEqual(python_result, rust_result)

    def test_rewrite_model_settings(self):
        python_result = _rewrite_model_settings(MODEL_SETTINGS_XML)
        rust_result = fuzzypaint_engine.rewrite_model_settings(MODEL_SETTINGS_XML)
        self.assertEqual(python_result, rust_result)


PLATE_JSON = (
    b'{"filament_colors":["#757575","#68724D"],"filament_ids":[0,1],'
    b'"first_extruder":1,"bbox_objects":[{"name":"model"},{"name":"wipe_tower"}]}'
)
SEQUENCE_JSON = b'{"1":{"sequence":[0,1,2],"nozzle_sequence":[0,1],"optimal_assignment":[1,0]}}'
PROJECT_SETTINGS_JSON = (
    b'{"filament_colour":["#757575","#68724D"],'
    b'"filament_type":["PLA","PLA"],'
    b'"different_settings_to_system":["a","b","c","d"],'
    b'"flush_volumes_matrix":[0,1,2,3],'
    b'"filament_minimal_purge_on_wipe_tower":["15","15"],'
    b'"enable_prime_tower":"1"}'
)


class JsonRewriteParityTests(unittest.TestCase):
    def test_rewrite_plate_json(self):
        python_result = _rewrite_plate_json(PLATE_JSON)
        rust_result = fuzzypaint_engine.rewrite_plate_json(PLATE_JSON)
        self.assertEqual(python_result, rust_result)

    def test_rewrite_sequence(self):
        python_result = _rewrite_sequence(SEQUENCE_JSON)
        rust_result = fuzzypaint_engine.rewrite_sequence(SEQUENCE_JSON)
        self.assertEqual(python_result, rust_result)

    def test_rewrite_project_settings(self):
        python_result = _rewrite_project_settings(PROJECT_SETTINGS_JSON)
        rust_result = fuzzypaint_engine.rewrite_project_settings(PROJECT_SETTINGS_JSON)
        self.assertEqual(python_result, rust_result)

    def test_rewrite_project_settings_with_non_ascii(self):
        data = b'{"filament_colour":["#757575","#68724D"],"notes":["caf\xc3\xa9"],"enable_prime_tower":"1"}'
        python_result = _rewrite_project_settings(data)
        rust_result = fuzzypaint_engine.rewrite_project_settings(data)
        self.assertEqual(python_result, rust_result)


if __name__ == "__main__":
    unittest.main()
