import tempfile
import unittest
import zipfile
import re
import json
import io
from pathlib import Path

from fuzzypaint import (
    ConversionError,
    ConversionReport,
    PaintNode,
    clean_sliced_output_base,
    convert_3mf,
    configure_project_for_slicing,
    convert_model_xml,
    _find_overlap_protection,
    decode_paint_stream,
    encode_paint_stream,
    leaf_states,
    next_available_destination,
    suggest_created_package_name,
    suggest_prepared_project_name,
    suggest_print_package_name,
)
from gcode_layer_detour import build_detours
from gcode_layer_interval_detour import build_interval_detours
from gcode_single_filament import (
    CollapseReport,
    _validate_tower_excursions,
    _find_tower_excursion_ranges,
    _rewrite_gcode_metadata,
    _rewrite_model_settings,
    _rewrite_plate_json,
    _rewrite_project_settings,
    _rewrite_sequence,
    _strip_marked_blocks,
    strip_nonprint_attachments,
)


class PaintCodecTests(unittest.TestCase):
    def test_whole_triangle_vectors(self):
        for state, encoded in [(0, "0"), (1, "4"), (2, "8"), (3, "0c"), (4, "1c"), (17, "ec"), (18, "0fc")]:
            node = PaintNode(state=state)
            self.assertEqual(encode_paint_stream(node), encoded)
            self.assertEqual(decode_paint_stream(encoded), node)

    def test_complex_stream_round_trip(self):
        raw = "0C0C80C0C30C0C3"
        tree = decode_paint_stream(raw)
        self.assertEqual(encode_paint_stream(tree).lower(), raw.lower())
        self.assertGreater(len(list(leaf_states(tree))), 1)

    def test_rejects_truncated_stream(self):
        with self.assertRaises(ConversionError):
            decode_paint_stream("c")


class XmlConversionTests(unittest.TestCase):
    def test_converts_selected_slot_and_removes_colour(self):
        xml = b'<triangles><triangle v1="0" v2="1" v3="2" paint_color="8"/></triangles>'
        converted, changed = convert_model_xml(xml, slots={2}, remove_colour=True)
        self.assertTrue(changed)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)
        self.assertNotIn(b"paint_color", converted)

    def test_keeps_colour_when_requested(self):
        xml = b"<triangle v1='0' v2='1' v3='2' paint_color='4'/>"
        converted, _ = convert_model_xml(xml, remove_colour=False)
        self.assertIn(b"paint_color='4'", converted)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_nonmatching_slot_keeps_colour_and_writes_no_fuzzy(self):
        xml = b'<triangle v1="0" v2="1" v3="2" paint_color="8"/>'
        converted, _ = convert_model_xml(xml, slots={1}, remove_colour=True)
        self.assertIn(b'paint_color="8"', converted)
        self.assertNotIn(b"paint_fuzzy_skin", converted)

    def test_selected_slot_is_removed_without_erasing_other_slots(self):
        colour_tree = PaintNode(split_code=1, children=(PaintNode(state=1), PaintNode(state=2)))
        raw = encode_paint_stream(colour_tree)
        xml = f'<triangle v1="0" v2="1" v3="2" paint_color="{raw}"/>'.encode()
        converted, _ = convert_model_xml(xml, slots={1}, remove_colour=True)
        colour_match = re.search(rb'paint_color="([0-9a-f]+)"', converted)
        fuzzy_match = re.search(rb'paint_fuzzy_skin="([0-9a-f]+)"', converted)
        self.assertEqual(list(leaf_states(decode_paint_stream(colour_match.group(1).decode()))), [0, 2])
        self.assertEqual(list(leaf_states(decode_paint_stream(fuzzy_match.group(1).decode()))), [1, 0])

    def test_preserves_existing_fuzzy_on_uncoloured_triangle(self):
        xml = b'<triangle v1="0" v2="1" v3="2" paint_fuzzy_skin="4"/>'
        converted, changed = convert_model_xml(xml)
        self.assertFalse(changed)
        self.assertEqual(converted, xml)

    def test_flat_contact_protects_actual_overlapping_face_footprint(self):
        # The small triangle lies against the middle of the painted face. None
        # of its vertices coincide with the painted triangle's vertices.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="0" y="10" z="0"/>
          <vertex x="2" y="2" z="0.02"/><vertex x="4" y="2" z="0.02"/>
          <vertex x="2" y="4" z="0.02"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_flat_contact=True,
            protect_overlap=False,
            protect_nonmanifold=False,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.flat_contact_triangles_protected, 1)
        self.assertAlmostEqual(report.flat_contact_area_protected_mm2, 50.0)

    def test_flat_contact_does_not_treat_shared_edge_as_surface_overlap(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="2" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_flat_contact=True,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertTrue(changed)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_flat_contact_rejects_parallel_faces_outside_gap(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="4" y="0" z="0"/>
          <vertex x="0" y="4" z="0"/>
          <vertex x="1" y="1" z="0.10"/><vertex x="2" y="1" z="0.10"/>
          <vertex x="1" y="2" z="0.10"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_flat_contact=True,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertTrue(changed)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_fuzz_envelope_protects_angled_face_that_can_poke_through_wall(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="-2" z="-2"/><vertex x="0" y="2" z="-2"/>
          <vertex x="0" y="0" z="2"/>
          <vertex x="0.02" y="-0.5" z="-0.5"/>
          <vertex x="0.02" y="0.5" z="-0.5"/>
          <vertex x="0.02" y="0" z="0.5"/>
          <vertex x="0.20" y="0" z="0.5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="3" v2="4" v3="5" paint_color="8"/>
          <triangle v1="3" v2="4" v3="6" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_flat_contact=True,
            protect_fuzz_envelope=True,
            fuzz_envelope_distance_mm=0.25,
            protect_overlap=False,
            protect_nonmanifold=False,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.flat_contact_triangles_protected, 1)
        self.assertEqual(report.fuzz_envelope_triangles_protected, 1)

    def test_fuzz_envelope_leaves_unobstructed_face_eligible(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_fuzz_envelope=True,
            fuzz_envelope_distance_mm=0.25,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertTrue(changed)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_planar_protection_withholds_large_coplanar_patch(self):
        xml = b'''<model><resources><object id="1"><mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object></resources></model>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml, slots={2}, protect_planar=True, report=report
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 1)
        self.assertEqual(report.planar_triangles_protected, 2)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 100.0)

    def test_planar_protection_keeps_small_patch_eligible(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(xml, slots={2}, protect_planar=True)
        self.assertTrue(changed)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_connected_coplanar_rule_protects_small_two_triangle_face(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="2" y="2" z="0"/><vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_connected_coplanar=True,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)

    def test_connected_coplanar_span_gate_rejects_small_face(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="2" y="2" z="0"/><vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_connected_coplanar=True,
            connected_coplanar_minimum_short_span_mm=3.0,
            connected_coplanar_minimum_long_span_mm=8.0,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertTrue(changed)
        self.assertEqual(converted.count(b"paint_fuzzy_skin"), 2)

    def test_connected_coplanar_span_gate_accepts_slot_sized_face(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="6" y="0" z="0"/>
          <vertex x="6" y="12" z="0"/><vertex x="0" y="12" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_connected_coplanar=True,
            connected_coplanar_minimum_short_span_mm=3.0,
            connected_coplanar_minimum_long_span_mm=8.0,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)

    def test_connected_coplanar_protects_nearby_fragment_of_qualifying_plane(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="6" y="0" z="0"/>
          <vertex x="6" y="12" z="0"/><vertex x="0" y="12" z="0"/>
          <vertex x="6.2" y="5" z="0"/><vertex x="7.2" y="5" z="0"/>
          <vertex x="7.2" y="6" z="0"/><vertex x="6.2" y="6" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="4" v2="6" v3="7" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_connected_coplanar=True,
            connected_coplanar_minimum_short_span_mm=3.0,
            connected_coplanar_minimum_long_span_mm=8.0,
            connected_coplanar_satellite_gap_mm=0.25,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)

    def test_connected_coplanar_rule_rejects_folded_pair(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="0" y="2" z="1"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_connected_coplanar=True,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertTrue(changed)
        self.assertEqual(converted.count(b"paint_fuzzy_skin"), 2)

    def test_connected_coplanar_rule_rejects_isolated_triangle(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_connected_coplanar=True,
            protect_overlap=False,
            protect_nonmanifold=False,
        )
        self.assertTrue(changed)
        self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_planar_protection_can_opt_in_compact_vertical_strip(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="3" z="0"/>
          <vertex x="0" y="3" z="13"/><vertex x="0" y="0" z="13"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_compact_strip_minimum_area_mm2=30.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 1)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 39.0)

    def test_planar_compact_strip_exception_rejects_horizontal_patch(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="13" y="0" z="0"/>
          <vertex x="13" y="3" z="0"/><vertex x="0" y="3" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_compact_strip_minimum_area_mm2=30.0,
        )
        self.assertTrue(changed)
        self.assertEqual(converted.count(b"paint_fuzzy_skin"), 2)

    def test_planar_protection_can_group_aligned_coplanar_lane(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="3" z="0"/>
          <vertex x="0" y="3" z="12"/><vertex x="0" y="0" z="12"/>
          <vertex x="0" y="0" z="17"/><vertex x="0" y="3" z="17"/>
          <vertex x="0" y="3" z="29"/><vertex x="0" y="0" z="29"/>
          <vertex x="0" y="0" z="34"/><vertex x="0" y="3" z="34"/>
          <vertex x="0" y="3" z="46"/><vertex x="0" y="0" z="46"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="4" v2="6" v3="7" paint_color="8"/>
          <triangle v1="8" v2="9" v3="10" paint_color="8"/>
          <triangle v1="8" v2="10" v3="11" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 3)
        self.assertEqual(report.planar_triangles_protected, 6)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 108.0)

    def test_planar_coplanar_group_rejects_broad_scattered_layout(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="6" z="0"/>
          <vertex x="0" y="6" z="6"/><vertex x="0" y="0" z="6"/>
          <vertex x="0" y="10" z="0"/><vertex x="0" y="16" z="0"/>
          <vertex x="0" y="16" z="6"/><vertex x="0" y="10" z="6"/>
          <vertex x="0" y="0" z="10"/><vertex x="0" y="6" z="10"/>
          <vertex x="0" y="6" z="16"/><vertex x="0" y="0" z="16"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="4" v2="6" v3="7" paint_color="8"/>
          <triangle v1="8" v2="9" v3="10" paint_color="8"/>
          <triangle v1="8" v2="10" v3="11" paint_color="8"/>
        </triangles></mesh></object>'''
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
        )
        self.assertTrue(changed)
        self.assertEqual(converted.count(b"paint_fuzzy_skin"), 6)

    def test_planar_directional_lane_axis_separates_parallel_lanes(self):
        vertices = []
        triangles = []
        for lane_y in (0, 10):
            for z_start in (0, 17, 34):
                base = len(vertices)
                vertices.extend(
                    [
                        (0, lane_y, z_start),
                        (0, lane_y + 3, z_start),
                        (0, lane_y + 3, z_start + 12),
                        (0, lane_y, z_start + 12),
                    ]
                )
                triangles.extend(((base, base + 1, base + 2), (base, base + 2, base + 3)))
        vertex_xml = b"".join(
            f'<vertex x="{x}" y="{y}" z="{z}"/>'.encode()
            for x, y, z in vertices
        )
        triangle_xml = b"".join(
            f'<triangle v1="{a}" v2="{b}" v3="{c}" paint_color="8"/>'.encode()
            for a, b, c in triangles
        )
        xml = (
            b'<object id="1"><mesh><vertices>'
            + vertex_xml
            + b'</vertices><triangles>'
            + triangle_xml
            + b'</triangles></mesh></object>'
        )

        generic, generic_changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
        )
        self.assertTrue(generic_changed)
        self.assertEqual(generic.count(b"paint_fuzzy_skin"), 12)

        directional, directional_changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
            planar_coplanar_lane_axis=[(1.0, 0.0, 0.0), (0.0, 0.0, 1.0)],
        )
        self.assertFalse(directional_changed)
        self.assertNotIn(b"paint_fuzzy_skin", directional)

    def test_planar_lane_accepts_one_direct_coplanar_satellite(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="3" z="0"/>
          <vertex x="0" y="3" z="40"/><vertex x="0" y="0" z="40"/>
          <vertex x="0" y="0" z="40.5"/><vertex x="0" y="3" z="40.5"/>
          <vertex x="0" y="3" z="46.5"/><vertex x="0" y="0" z="46.5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="4" v2="6" v3="7" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 2)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 138.0)

    def test_planar_strip_accepts_sub_five_mm2_disconnected_fragment(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="3" z="0"/>
          <vertex x="0" y="3" z="12"/><vertex x="0" y="0" z="12"/>
          <vertex x="0" y="0" z="12.5"/><vertex x="0" y="0.75" z="12.5"/>
          <vertex x="0" y="0" z="13.5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_compact_strip_minimum_area_mm2=30.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
            protect_overlap=False,
            protect_nonmanifold=False,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 2)

    def test_planar_compact_strip_accepts_one_sparse_coplanar_satellite(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="3" z="0"/>
          <vertex x="0" y="3" z="12"/><vertex x="0" y="0" z="12"/>
          <vertex x="0" y="0" z="12.5"/><vertex x="0" y="3" z="12.5"/>
          <vertex x="0" y="0" z="16.5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_compact_strip_minimum_area_mm2=30.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 2)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 42.0)

    def test_planar_layered_feature_protects_shallow_parallel_faces(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="0" y="3" z="0"/>
          <vertex x="0" y="0" z="4"/>
          <vertex x="0.5" y="0" z="0"/><vertex x="0.5" y="3" z="0"/>
          <vertex x="0.5" y="0" z="4"/><vertex x="0.25" y="0" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5" paint_color="8"/>
          <triangle v1="3" v2="4" v3="6" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=100.0,
            planar_coplanar_lane_minimum_patch_area_mm2=5.0,
            planar_layered_feature_maximum_depth_mm=1.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 3)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 12.375)

    def test_planar_protection_merges_across_duplicated_seam_vertices(self):
        # Two coplanar triangles forming one 10x10 flat square (100mm2), but
        # exported the way many tools split a UV/hard-normal seam: the second
        # triangle references *duplicate* vertex entries (indices 4, 5) for
        # the same two physical points the first triangle uses (indices 0, 2),
        # rather than reusing those same index numbers. Each triangle alone is
        # only 50mm2, under this test's 60mm2 threshold -- so this only passes
        # if connectivity is matched by vertex position, not raw index, and
        # the two triangles are recognised as one connected 100mm2 patch.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="10" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="4" v2="5" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=60.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 1)
        self.assertEqual(report.planar_triangles_protected, 2)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 100.0)

    def test_planar_protection_classifies_geometry_across_paint_boundary(self):
        # The two triangles form one 100mm2 geometric face, but only one is
        # painted. The painted half is below the 60mm2 threshold by itself;
        # it is protected only if patch discovery uses the complete geometry
        # and applies the paint selection after classifying the flat patch.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_minimum_area_mm2=60.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 1)
        self.assertEqual(report.planar_triangles_protected, 1)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 50.0)

    def test_planar_protection_absorbs_local_noise_via_patch_average(self):
        # Three triangles, H-A-B, sharing edges in a chain (H-A, then A-B --
        # B does NOT touch H directly). H is flat (20mm2). A is tilted ~4.0
        # degrees off H. B is tilted ~4.2 degrees off H, but in a different
        # rotational direction, such that B is ~8.0 degrees off A SPECIFICALLY
        # (verified numerically: dot(A,B) = 0.9904 < cos(5deg) = 0.9962).
        # With a pure pairwise (immediate-neighbour-only) comparison, B's only
        # entry point -- the A-B edge -- fails the 5 degree tolerance, leaving
        # B an isolated ~4mm2 patch (fuzzed, under the 15mm2 threshold) even
        # though B itself is well within 5 degrees of the *overall* flat
        # direction. Comparing B against the patch's accumulated average
        # (here: H and A merged, area-weighted average dominated by H's
        # 20mm2) instead of against A's specific normal lets B merge in too
        # (verified: dot(average, B) = 0.9965 >= 0.9962), keeping the whole
        # 28mm2 patch protected as the real surface clearly is.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="8" y="0" z="0"/>
          <vertex x="0" y="5" z="0"/><vertex x="4" y="-1" z="0.07"/>
          <vertex x="4" y="-3" z="-0.06"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="0" v3="3" paint_color="8"/>
          <triangle v1="1" v2="3" v3="4" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_angle_degrees=5.0,
            planar_minimum_area_mm2=15.0,
            report=report,
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 1)
        self.assertEqual(report.planar_triangles_protected, 3)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 28.020501801195622, places=6)

    def test_planar_protection_does_not_absorb_angled_small_island(self):
        # H is a flat 10x10mm square (two triangles, 100mm2 total) -- well
        # over the 15mm2 threshold on its own. S is a small (~5.6mm2)
        # triangle sharing an edge with H but tilted ~63 degrees off H's
        # normal (dot = -0.447), nowhere close to the 7 degree tolerance, so
        # it correctly fails to merge into H's patch during angle-based
        # growth. S must remain eligible for fuzz: recursively absorbing
        # angle-separated neighbours lets one large seed swallow an entire
        # connected model and is the source of the excessive protection this
        # regression test guards against.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
          <vertex x="10.5" y="5" z="1"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
          <triangle v1="1" v2="2" v3="4" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_angle_degrees=7.0,
            planar_minimum_area_mm2=15.0,
            report=report,
        )
        self.assertTrue(changed)
        self.assertEqual(converted.count(b"paint_fuzzy_skin"), 1)
        self.assertEqual(report.planar_patches_protected, 1)
        self.assertEqual(report.planar_triangles_protected, 2)
        self.assertAlmostEqual(report.planar_area_protected_mm2, 100.0, places=6)

    def test_planar_protection_leaves_isolated_small_patch_unprotected(self):
        # A small (2mm2) triangle with no large patch anywhere nearby to be
        # absorbed into -- it should remain fuzzed, same as before the
        # absorption pass existed.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml,
            slots={2},
            protect_planar=True,
            planar_angle_degrees=7.0,
            planar_minimum_area_mm2=15.0,
            report=report,
        )
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.planar_patches_protected, 0)
        self.assertEqual(report.planar_triangles_protected, 0)

    def test_overlap_protection_skips_painted_triangle_hit_by_a_colliding_spike(self):
        # A painted 2mm2 triangle (vertices 0,1,2) and a completely separate,
        # unpainted "spike" triangle (vertices 3,4,5, sharing no vertex
        # position with the painted one at all) whose tip sits only 0.1mm
        # from the painted triangle's centroid -- the real-world shape of a
        # stray, never-welded mesh chunk colliding with a wall at a single
        # point, confirmed by the user dragging actual offending chunks away
        # from a real model in Blender and seeing the collision disappear.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="0.667" y="0.667" z="0.1"/>
          <vertex x="3" y="3" z="5"/><vertex x="3" y="-3" z="5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(xml, slots={2}, report=report)
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.overlap_triangles_protected, 1)
        self.assertAlmostEqual(report.overlap_area_protected_mm2, 2.0)

    def test_overlap_protection_ignores_a_distant_unrelated_triangle(self):
        # Same shapes as above, but the second triangle is far away -- no
        # collision, so the painted triangle should fuzz normally.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="100" y="100" z="100"/>
          <vertex x="103" y="100" z="100"/><vertex x="100" y="103" z="100"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(xml, slots={2}, report=report)
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.overlap_triangles_protected, 0)

    def test_overlap_protection_leaves_an_ordinary_shared_edge_alone(self):
        # Two triangles sharing a real mesh edge (the same connected piece,
        # like any ordinary flat patch) touch at that edge by definition --
        # that must never be treated as a collision, however close.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(xml, slots={2}, report=report)
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.overlap_triangles_protected, 0)

    def test_overlap_protection_can_be_disabled(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="0.667" y="0.667" z="0.1"/>
          <vertex x="3" y="3" z="5"/><vertex x="3" y="-3" z="5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml, slots={2}, protect_overlap=False, report=report
        )
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.overlap_triangles_protected, 0)

    def test_overlap_protection_depth_mm_is_opt_in_and_stays_within_one_shell_pair(self):
        # depth_mm defaults to max_gap_mm (a no-op) because raising it was
        # tried on a real model and rejected -- see _find_overlap_protection's
        # docstring: kitbashed rocks sit close to their own neighbours across
        # most of their surface, so a wider Euclidean radius does not find
        # just an embedded tip, it swallows whole decorative chunks. The
        # parameter is kept, unused by default, in case a future geodesic
        # (surface-distance) version wants it. This test exercises the
        # low-level function directly with an explicit depth_mm, since the
        # public pipeline no longer passes anything but the default.
        #
        # Triangle 0 (A) is the wall. Triangle 1 (B1) touches A at a single
        # point near A's corner. Triangle 2 (B2) shares a vertex position
        # with B1 (same shell) but is ~1.4mm from A -- farther than
        # max_gap_mm (0.3) but within an explicit depth_mm=3.0. Triangle 3
        # (C) sits close to B2 in space but was never itself confirmed
        # touching anything -- it must stay unprotected even at depth_mm=3.0,
        # because depth only extends along a shell already proven colliding
        # with a *specific* other shell, not a blanket "anything nearby".
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/>
          <vertex x="0.05" y="0.05" z="0.05"/><vertex x="0.3" y="0.05" z="0.05"/><vertex x="0.05" y="0.3" z="0.05"/>
          <vertex x="0.3" y="0.05" z="0.05"/><vertex x="0.3" y="0.05" z="2.0"/><vertex x="0.5" y="0.05" z="2.0"/>
          <vertex x="0.3" y="0.05" z="3.5"/><vertex x="0.5" y="0.05" z="3.5"/><vertex x="0.4" y="0.3" z="3.5"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2"/>
          <triangle v1="3" v2="4" v3="5"/>
          <triangle v1="6" v2="7" v3="8"/>
          <triangle v1="9" v2="10" v3="11"/>
        </triangles></mesh></object>'''
        protected, _ = _find_overlap_protection(xml, depth_mm=3.0)
        self.assertEqual(protected, {0, 1, 2})

    def test_coincident_protection_skips_a_whole_flat_patch_on_both_sides(self):
        # Shell A (a flat 2x2 square, triangles 0+1 sharing an edge) and
        # shell B (a second flat 2x2 square, triangles 2+3 sharing an edge)
        # sit almost exactly on top of each other -- B is offset only
        # 0.02mm in z, a different shell (no shared vertex positions with
        # A), the real shape of a mortise joint's own flat wall sitting
        # coincident with whatever meets it. Both patches must be fully
        # protected, not just the two seed triangles that triggered
        # detection -- confirmed by the whole 2x2 square on each side
        # being skipped, not merely the first triangle of each.
        # Flatness+coincidence alone is no longer enough -- three extra,
        # unpainted triangles (8,9,10 / 8,9,11 / 8,9,12) share one edge
        # (8-9) among themselves, making it non-manifold. They sit right
        # next to the coincident squares (just outside their footprint,
        # at the same z height) but share no vertex with either -- the
        # corroborating "there is scarred/debris geometry right here"
        # evidence the check now requires, found purely by proximity, not
        # by being part of either shell.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/><vertex x="0" y="2" z="0"/><vertex x="2" y="2" z="0"/>
          <vertex x="0" y="0" z="0.02"/><vertex x="2" y="0" z="0.02"/><vertex x="0" y="2" z="0.02"/><vertex x="2" y="2" z="0.02"/>
          <vertex x="0.5" y="-0.5" z="0.02"/><vertex x="1" y="-1" z="0.02"/>
          <vertex x="1.5" y="-0.5" z="0.02"/><vertex x="2" y="-1" z="0.02"/><vertex x="2.5" y="-0.5" z="0.02"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="5" v2="7" v3="6" paint_color="8"/>
          <triangle v1="8" v2="9" v3="10"/>
          <triangle v1="8" v2="9" v3="11"/>
          <triangle v1="8" v2="9" v3="12"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml, slots={2}, protect_coincident=True, report=report
        )
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.coincident_triangles_protected, 4)

    def test_coincident_protection_ignores_a_nearby_differently_angled_shell(self):
        # Same coincident pair as above, plus a third shell (C, triangle 4)
        # that sits spatially close to A/B but lies in a completely
        # different plane (perpendicular, not coplanar) -- an ordinary
        # adjacent wall meeting this one at a corner, not a second surface
        # sitting on top of it. C must stay fuzzed. Same evidence
        # triangles as the first test (see its comment) so A/B still pass
        # the non-manifold-evidence requirement.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/><vertex x="0" y="2" z="0"/><vertex x="2" y="2" z="0"/>
          <vertex x="0" y="0" z="0.02"/><vertex x="2" y="0" z="0.02"/><vertex x="0" y="2" z="0.02"/><vertex x="2" y="2" z="0.02"/>
          <vertex x="0" y="0" z="0.1"/><vertex x="2" y="0" z="0.1"/><vertex x="0" y="0" z="2.1"/>
          <vertex x="0.5" y="-0.5" z="0.02"/><vertex x="1" y="-1" z="0.02"/>
          <vertex x="1.5" y="-0.5" z="0.02"/><vertex x="2" y="-1" z="0.02"/><vertex x="2.5" y="-0.5" z="0.02"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="5" v2="7" v3="6" paint_color="8"/>
          <triangle v1="8" v2="9" v3="10" paint_color="8"/>
          <triangle v1="11" v2="12" v3="13"/>
          <triangle v1="11" v2="12" v3="14"/>
          <triangle v1="11" v2="12" v3="15"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml, slots={2}, protect_coincident=True, report=report
        )
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.coincident_triangles_protected, 4)

    def test_coincident_protection_can_be_disabled(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/><vertex x="0" y="2" z="0"/><vertex x="2" y="2" z="0"/>
          <vertex x="0" y="0" z="0.02"/><vertex x="2" y="0" z="0.02"/><vertex x="0" y="2" z="0.02"/><vertex x="2" y="2" z="0.02"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
          <triangle v1="5" v2="7" v3="6" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml, slots={2}, protect_coincident=False, report=report
        )
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.coincident_triangles_protected, 0)

    def test_nonmanifold_protection_skips_a_painted_triangle_on_a_shared_edge(self):
        # Three triangles (A painted, B and C unpainted) all share the same
        # edge (v0-v1) -- a real edge should only ever be shared by two
        # triangles (or one, at an open boundary); a third triangle on it
        # means something extra was stitched on right there, the real
        # signature of a scarred-in remnant left behind by a since-removed
        # colliding chunk.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/><vertex x="0" y="-1" z="0"/>
          <vertex x="0" y="0" z="1"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="1" v3="3"/>
          <triangle v1="0" v2="1" v3="4"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(xml, slots={2}, report=report)
        self.assertFalse(changed)
        self.assertNotIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.nonmanifold_triangles_protected, 1)
        self.assertAlmostEqual(report.nonmanifold_area_protected_mm2, 0.5)

    def test_nonmanifold_protection_leaves_an_ordinary_shared_edge_alone(self):
        # A normal edge shared by exactly two triangles (any ordinary flat
        # patch) is not a defect and must fuzz normally.
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="10" y="10" z="0"/><vertex x="0" y="10" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="2" v3="3" paint_color="8"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(xml, slots={2}, report=report)
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.nonmanifold_triangles_protected, 0)

    def test_nonmanifold_protection_can_be_disabled(self):
        xml = b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/><vertex x="0" y="-1" z="0"/>
          <vertex x="0" y="0" z="1"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="0" v2="1" v3="3"/>
          <triangle v1="0" v2="1" v3="4"/>
        </triangles></mesh></object>'''
        report = ConversionReport()
        converted, changed = convert_model_xml(
            xml, slots={2}, protect_nonmanifold=False, report=report
        )
        self.assertTrue(changed)
        self.assertIn(b"paint_fuzzy_skin", converted)
        self.assertEqual(report.nonmanifold_triangles_protected, 0)


class ArchiveTests(unittest.TestCase):
    def test_step_one_explains_when_a_sliced_file_was_selected(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            output = Path(folder) / "output.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "3D/Objects/object_1.model",
                    b'<model><triangle v1="0" v2="1" v3="2"/></model>',
                )
                archive.writestr("Metadata/plate_1.gcode", b"G1 X1 Y1\n")
            with self.assertRaisesRegex(ConversionError, "already-sliced"):
                convert_3mf(source, output)

    def test_tower_trip_removal_also_removes_its_orphaned_unretraction(self):
        source = io.BytesIO(b"""; CP TOOLCHANGE START
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
""")
        destination = io.BytesIO()
        report = CollapseReport()
        _strip_marked_blocks(source, destination, report, skip_ranges=[(4, 8)])
        result = destination.getvalue()
        self.assertIn(b"G1 X100 Y100 F30000", result)
        self.assertNotIn(b"G1 E.8 F1800", result)
        self.assertIn(b"G1 X101 Y100 E.1", result)
        self.assertEqual(report.orphan_unretractions_removed, 1)

    def test_travel_only_tower_range_keeps_following_unretraction(self):
        source = io.BytesIO(b"""; CP TOOLCHANGE START
G1 E-2
; CP TOOLCHANGE END
G1 X220 Y220
; OBJECT_ID: 1
G1 X100 Y100 F30000
G1 Z.2
G1 E.8 F1800
; FEATURE: Outer wall
G1 X101 Y100 E.1
""")
        destination = io.BytesIO()
        report = CollapseReport()
        _strip_marked_blocks(source, destination, report, skip_ranges=[(4, 4)])
        result = destination.getvalue()
        self.assertIn(b"G1 E.8 F1800", result)
        self.assertEqual(report.orphan_unretractions_removed, 0)

    def test_finds_unmarked_travel_wrapping_a_wipe_tower(self):
        gcode = b"""G90
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
        self.assertEqual(_find_tower_excursion_ranges(io.BytesIO(gcode)), [(4, 14)])

    def test_finds_departure_when_tower_starts_inside_toolchange_block(self):
        gcode = b"""G90
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
; FEATURE: Prime tower
; WIPE_TOWER_START
G1 X224 Y224 E1
; WIPE_TOWER_END
; OBJECT_ID: 1
G1 X101 Y131
"""
        self.assertEqual(_find_tower_excursion_ranges(io.BytesIO(gcode)), [(5, 15)])

    def test_tower_validation_allows_tower_layers_without_colour_changes(self):
        report = CollapseReport(
            toolchanges_removed=112,
            tower_blocks_removed=118,
            tower_excursions_removed=118,
        )
        _validate_tower_excursions(report)

    def test_tower_validation_rejects_an_unmatched_standalone_block(self):
        report = CollapseReport(
            toolchanges_removed=112,
            tower_blocks_removed=118,
            tower_excursions_removed=117,
        )
        with self.assertRaisesRegex(
            ConversionError, "117 journeys for 118 standalone prime-tower blocks"
        ):
            _validate_tower_excursions(report)

    def test_slim_package_keeps_print_data_and_thumbnails(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            output = Path(folder) / "slim.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", b"G1 X1 Y1\n")
                archive.writestr("Metadata/project_settings.config", b"{}")
                archive.writestr("Auxiliaries/.thumbnails/thumbnail.png", b"thumb")
                archive.writestr("Auxiliaries/Others/manual.pdf", b"manual")
            removed, removed_bytes = strip_nonprint_attachments(source, output)
            self.assertEqual((removed, removed_bytes), (1, 6))
            with zipfile.ZipFile(output) as archive:
                self.assertIn("Metadata/plate_1.gcode", archive.namelist())
                self.assertIn("Metadata/project_settings.config", archive.namelist())
                self.assertIn("Auxiliaries/.thumbnails/thumbnail.png", archive.namelist())
                self.assertNotIn("Auxiliaries/Others/manual.pdf", archive.namelist())

    def test_fuzzy_process_settings_are_written_without_losing_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "output.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr(
                    "Metadata/project_settings.config",
                    json.dumps({"keep": "yes", "fuzzy_skin_thickness": "0.1"}),
                )
            convert_3mf(
                source,
                output,
                fuzzy_settings={
                    "fuzzy_skin_thickness": 0.3,
                    "fuzzy_skin_point_distance": 0.8,
                },
            )
            with zipfile.ZipFile(output) as archive:
                settings = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertEqual(settings["keep"], "yes")
            self.assertEqual(settings["fuzzy_skin_thickness"], "0.3")
            self.assertEqual(settings["fuzzy_skin_point_distance"], "0.8")
            process_overrides = set(
                settings["different_settings_to_system"][0].split(";")
            )
            self.assertEqual(
                process_overrides,
                {"fuzzy_skin_thickness", "fuzzy_skin_point_distance"},
            )

    def test_disables_wipe_for_every_filament_without_changing_other_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "output.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            settings = {
                "wipe": ["1", "1"],
                "filament_wipe": ["0", "nil", "1"],
                "filament_settings_id": ["PLA", "PETG", "TPU"],
                "different_settings_to_system": ["keep_process", "", "keep_filament", "", "keep_printer"],
                "wipe_distance": ["2", "3"],
                "retraction_length": ["0.8", "0.4"],
                "unrelated": "preserved",
            }
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
            report = convert_3mf(
                source, output, disable_wipe_while_retracting=True
            )
            with zipfile.ZipFile(output) as archive:
                result = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertTrue(report.wipe_while_retracting_disabled)
            self.assertEqual(result["wipe"], ["0", "0"])
            self.assertEqual(result["filament_wipe"], ["0", "0", "0"])
            self.assertEqual(result["wipe_distance"], ["2", "3"])
            self.assertEqual(result["retraction_length"], ["0.8", "0.4"])
            self.assertEqual(result["unrelated"], "preserved")
            differences = result["different_settings_to_system"]
            self.assertEqual(set(differences[0].split(";")), {"keep_process", "wipe"})
            self.assertEqual(differences[1], "filament_wipe")
            self.assertEqual(
                set(differences[2].split(";")), {"filament_wipe", "keep_filament"}
            )
            self.assertEqual(differences[3], "filament_wipe")
            self.assertEqual(differences[4], "keep_printer")

    def test_disables_retractions_for_process_and_every_filament(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "output.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            settings = {
                "filament_settings_id": ["PLA", "PETG"],
                "different_settings_to_system": ["keep_process", "", "keep_filament", "keep_printer"],
                "retraction_length": ["0.8", "0.6"],
                "filament_retraction_length": ["nil", "0.4", "0.7", "nil"],
                "use_firmware_retraction": "1",
                "retraction_speed": ["30", "40"],
                "retract_length_toolchange": ["2", "2"],
            }
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
            report = convert_3mf(source, output, disable_retractions=True)
            with zipfile.ZipFile(output) as archive:
                result = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertTrue(report.retractions_disabled)
            self.assertEqual(result["retraction_length"], ["0", "0"])
            self.assertEqual(result["filament_retraction_length"], ["0", "0", "0", "0"])
            self.assertEqual(result["use_firmware_retraction"], "0")
            self.assertEqual(result["retraction_speed"], ["30", "40"])
            self.assertEqual(result["retract_length_toolchange"], ["2", "2"])
            differences = result["different_settings_to_system"]
            self.assertEqual(
                set(differences[0].split(";")),
                {"keep_process", "retraction_length", "use_firmware_retraction"},
            )
            self.assertEqual(differences[1], "filament_retraction_length")
            self.assertEqual(
                set(differences[2].split(";")),
                {"filament_retraction_length", "keep_filament"},
            )
            self.assertEqual(differences[3], "keep_printer")

    def test_slicing_copy_can_disable_retractions_without_touching_model(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "prepared.3mf"
            output = Path(folder) / "slice-input.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_fuzzy_skin="4"/></model>'
            settings = {
                "filament_settings_id": ["PLA", "PETG"],
                "different_settings_to_system": ["", "", "", ""],
                "retraction_length": ["0.8", "0.6"],
                "filament_retraction_length": ["nil", "0.4", "0.7", "nil"],
                "use_firmware_retraction": "1",
            }
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
                archive.writestr("Auxiliaries/.thumbnails/thumbnail.png", b"thumbnail")
            configure_project_for_slicing(source, output, disable_retractions=True)
            with zipfile.ZipFile(output) as archive:
                result = json.loads(archive.read("Metadata/project_settings.config"))
                self.assertEqual(archive.read("3D/Objects/object_1.model"), model)
                self.assertEqual(
                    archive.read("Auxiliaries/.thumbnails/thumbnail.png"), b"thumbnail"
                )
            self.assertEqual(result["retraction_length"], ["0", "0"])
            self.assertEqual(result["filament_retraction_length"], ["0", "0", "0", "0"])
            self.assertEqual(result["use_firmware_retraction"], "0")

    def test_slicing_copy_can_also_force_wipe_and_z_hop_together(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "prepared.3mf"
            output = Path(folder) / "slice-input.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_fuzzy_skin="4"/></model>'
            settings = {
                "filament_settings_id": ["PLA", "PETG"],
                "different_settings_to_system": ["", "", "", ""],
                "retraction_length": ["0.8", "0.6"],
                "filament_retraction_length": ["nil", "0.4", "0.7", "nil"],
                "use_firmware_retraction": "1",
                "wipe": ["1", "1"],
                "filament_wipe": ["nil", "1", "1", "nil"],
                "z_hop_types": ["Auto Lift", "Normal Lift"],
                "filament_z_hop_types": ["nil", "Auto Lift", "Normal Lift", "nil"],
                "z_hop": ["0.4", "0.6"],
            }
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
            configure_project_for_slicing(
                source,
                output,
                disable_retractions=True,
                disable_wipe_while_retracting=True,
                force_sloped_z_hop=True,
            )
            with zipfile.ZipFile(output) as archive:
                result = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertEqual(result["retraction_length"], ["0", "0"])
            self.assertEqual(result["wipe"], ["0", "0"])
            self.assertEqual(result["filament_wipe"], ["0", "0", "0", "0"])
            self.assertEqual(result["z_hop_types"], ["Slope Lift", "Slope Lift"])
            self.assertEqual(
                result["filament_z_hop_types"], ["Slope Lift", "Slope Lift", "Slope Lift", "Slope Lift"]
            )
            # Z-hop heights themselves are left untouched by the sloped-lift switch.
            self.assertEqual(result["z_hop"], ["0.4", "0.6"])

    def test_forces_sloped_z_hop_without_changing_heights_or_retraction(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "output.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            settings = {
                "z_hop_types": ["Auto Lift", "Normal Lift"],
                "filament_z_hop_types": ["nil", "Auto Lift", "Slope Lift"],
                "filament_settings_id": ["PLA", "PETG", "TPU"],
                "different_settings_to_system": ["", "", "", "", ""],
                "z_hop": ["0.4", "0"],
                "filament_z_hop": ["0.6", "nil", "0.2"],
                "retraction_length": ["0.8", "0.4"],
                "unrelated": "preserved",
            }
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
            report = convert_3mf(source, output, force_sloped_z_hop=True)
            with zipfile.ZipFile(output) as archive:
                result = json.loads(archive.read("Metadata/project_settings.config"))
            self.assertTrue(report.sloped_z_hop_enabled)
            self.assertEqual(result["z_hop_types"], ["Slope Lift", "Slope Lift"])
            self.assertEqual(
                result["filament_z_hop_types"],
                ["Slope Lift", "Slope Lift", "Slope Lift"],
            )
            self.assertEqual(result["z_hop"], ["0.4", "0"])
            self.assertEqual(result["filament_z_hop"], ["0.6", "nil", "0.2"])
            self.assertEqual(result["retraction_length"], ["0.8", "0.4"])
            self.assertEqual(result["unrelated"], "preserved")
            differences = result["different_settings_to_system"]
            self.assertEqual(differences[0], "z_hop_types")
            self.assertEqual(differences[1:4], ["filament_z_hop_types"] * 3)

    def test_archive_round_trip_preserves_other_members(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "output.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", b'{"example":true}')
            report = convert_3mf(source, output)
            self.assertEqual(report.fuzzy_triangles_written, 1)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.read("Metadata/project_settings.config"), b'{"example":true}')
                converted = archive.read("3D/Objects/object_1.model")
                self.assertIn(b'paint_fuzzy_skin="4"', converted)

    def test_prepared_copy_can_drop_nonprint_auxiliaries_but_keep_thumbnails(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "output.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Auxiliaries/Assembly Guide/manual.pdf", b"guide")
                archive.writestr("Auxiliaries/.thumbnails/thumbnail.png", b"thumb")
            report = convert_3mf(source, output, strip_nonprint_auxiliaries=True)
            self.assertEqual(report.attachments_removed, 1)
            self.assertEqual(report.attachment_bytes_removed, 5)
            with zipfile.ZipFile(output) as archive:
                self.assertNotIn("Auxiliaries/Assembly Guide/manual.pdf", archive.namelist())
                self.assertEqual(
                    archive.read("Auxiliaries/.thumbnails/thumbnail.png"), b"thumb"
                )

    def test_single_colour_output_keeps_fuzzy_and_remaps_parts(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "single.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            settings = b'''<config><object id="1">
              <metadata key="extruder" value="2"/>
              <part id="1"><metadata key="extruder" value="2"/></part>
            </object></config>'''
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/model_settings.config", settings)
                archive.writestr("Metadata/project_settings.config", b'{"keep":true}')
            report = convert_3mf(source, output, slots={2}, single_colour=True)
            self.assertEqual(report.single_colour_attributes_removed, 1)
            self.assertEqual(report.single_colour_assignments_remapped, 2)
            with zipfile.ZipFile(output) as archive:
                converted = archive.read("3D/Objects/object_1.model")
                remapped = archive.read("Metadata/model_settings.config")
                self.assertIn(b'paint_fuzzy_skin="4"', converted)
                self.assertNotIn(b"paint_color", converted)
                self.assertNotIn(b'value="2"', remapped)
                self.assertEqual(archive.read("Metadata/project_settings.config"), b'{"keep":true}')

    def test_single_spool_segmented_keeps_regions_and_removes_purge_costs(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.3mf"
            output = Path(folder) / "segmented.3mf"
            model = b'<model><triangle v1="0" v2="1" v3="2" paint_color="8"/></model>'
            settings = {
                "enable_prime_tower": "1",
                "flush_volumes_matrix": ["0", "123", "143", "0"],
                "flush_volumes_vector": ["140", "140", "140", "140"],
                "filament_minimal_purge_on_wipe_tower": ["15", "15"],
                "unrelated": "preserved",
            }
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("3D/Objects/object_1.model", model)
                archive.writestr("Metadata/project_settings.config", json.dumps(settings))
            report = convert_3mf(
                source, output, slots={2}, single_spool_segmented=True
            )
            self.assertEqual(report.single_spool_pairs_zeroed, 2)
            with zipfile.ZipFile(output) as archive:
                converted = archive.read("3D/Objects/object_1.model")
                result = json.loads(archive.read("Metadata/project_settings.config"))
                self.assertIn(b'paint_color="8"', converted)
                self.assertIn(b'paint_fuzzy_skin="4"', converted)
                self.assertEqual(result["enable_prime_tower"], "0")
                self.assertEqual(result["flush_volumes_matrix"], ["0", "0", "0", "0"])
                self.assertEqual(result["unrelated"], "preserved")


class LiteralDetourTests(unittest.TestCase):
    def test_cross_layer_colour_boundary_creates_in_span_out_path(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T0
; FEATURE: Outer wall
G1 X1 Y0 E1
; FEATURE: Inner wall
G1 X1 Y0.5 E1
; CHANGE_LAYER
T1
G0 X0 Y0
; FEATURE: Outer wall
G1 X1 Y0 E1
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X1 Y0.5 E1
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_detours(source, inset_mm=0.24)

        self.assertEqual(len(replacements), 1)
        emitted = next(iter(replacements.values())).decode()
        self.assertIn("X0.00000 Y0.24000", emitted)
        self.assertIn("X1.00000 Y0.24000", emitted)
        self.assertIn("X1.00000 Y0.00000", emitted)
        self.assertIn("FuzzyPaint detour in", emitted)
        self.assertIn("FuzzyPaint inset span", emitted)
        self.assertIn("FuzzyPaint detour out", emitted)

    def test_short_upper_island_moves_detour_to_longer_lower_run(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T0
; FEATURE: Outer wall
G1 X2 Y0 E2
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X2 Y0.5 E2
; CHANGE_LAYER
T1
G0 X0.6 Y0
; FEATURE: Outer wall
G1 X1.4 Y0 E0.8
G0 X0.6 Y0.5
; FEATURE: Inner wall
G1 X1.4 Y0.5 E0.8
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_detours(source, inset_mm=0.24)

        self.assertEqual(len(replacements), 1)
        emitted = next(iter(replacements.values())).decode()
        self.assertIn("X2.00000 Y0.24000", emitted)
        self.assertNotIn("X1.40000 Y0.24000", emitted)

    def test_submillimetre_endpoint_overlap_is_detected_by_sampling(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T0
G0 X1.9 Y0
; FEATURE: Outer wall
G1 X2.1 Y0 E0.2
G0 X1.9 Y0.5
; FEATURE: Inner wall
G1 X2.1 Y0.5 E0.2
; CHANGE_LAYER
T1
G0 X0 Y0
; FEATURE: Outer wall
G1 X2 Y0 E2
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X2 Y0.5 E2
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_detours(
                source, inset_mm=0.24, boundary_resolution_mm=0.10
            )

        self.assertEqual(len(replacements), 1)
        self.assertIn(
            "FuzzyPaint detour in",
            next(iter(replacements.values())).decode(),
        )

    def test_interval_clipping_does_not_shift_the_whole_upper_segment(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T0
G0 X0.9 Y0
; FEATURE: Outer wall
G1 X1.1 Y0 E0.2
G0 X0.9 Y0.5
; FEATURE: Inner wall
G1 X1.1 Y0.5 E0.2
; CHANGE_LAYER
T1
G0 X0 Y0
; FEATURE: Outer wall
G1 X2 Y0 E2
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X2 Y0.5 E2
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_interval_detours(
                source, inset_mm=0.24, boundary_resolution_mm=0.10
            )

        emitted = next(iter(replacements.values())).decode().splitlines()
        inset_x = [
            float(re.search(r" X([0-9.]+)", line).group(1))
            for line in emitted if "clipped inset span" in line
        ]
        # The physical transition extends beyond the colour core, but the
        # detour still leaves unmodified path at both ends of this 2 mm line.
        self.assertGreater(min(inset_x), 0.0)
        self.assertLessEqual(max(inset_x), 2.0)
        inset_y = [
            float(re.search(r" Y([0-9.]+)", line).group(1))
            for line in emitted if "clipped inset span" in line
        ]
        self.assertTrue(any(0.0 < value < 0.24 for value in inset_y))
        self.assertLess(inset_y[0], 0.05)
        self.assertLess(inset_y[-1], 0.05)

    def test_same_layer_colour_interface_creates_endpoint_channel(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T0
; FEATURE: Outer wall
G1 X1 Y0 E1
T1
G0 X1.4 Y0
; FEATURE: Outer wall
G1 X2.4 Y0 E1
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_interval_detours(
                source, same_layer_gap_mm=0.08
            )

        self.assertEqual(len(replacements), 2)
        emitted = b"".join(replacements.values()).decode()
        self.assertIn("X0.96000 Y0.00000", emitted)
        self.assertIn("X1.44000 Y0.00000", emitted)
        self.assertIn("FuzzyPaint same-layer channel", emitted)

    def test_targeted_lower_mode_detours_line_below_opposite_colour(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T0
; FEATURE: Outer wall
G1 X1 Y0 E1
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X1 Y0.5 E1
; CHANGE_LAYER
T1
G0 X0 Y0
; FEATURE: Outer wall
G1 X1 Y0 E1
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X1 Y0.5 E1
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_interval_detours(
                source,
                detour_side="lower",
                target_layers={1},
                same_layer_gap_mm=0,
            )

        self.assertEqual(len(replacements), 1)
        self.assertLess(next(iter(replacements)), 10)
        emitted = next(iter(replacements.values())).decode()
        self.assertIn("FuzzyPaint tapered detour in", emitted)
        self.assertIn("Y0.24000", emitted)
        connector_lines = [line for line in emitted.splitlines() if "tapered detour" in line]
        self.assertTrue(connector_lines)
        self.assertTrue(all(line.startswith("G0 ") and " E" not in line for line in connector_lines))
        extrusion = sum(float(value) for value in re.findall(r"\bE([0-9.]+)", emitted))
        self.assertAlmostEqual(extrusion, 1.0, places=5)

    def test_both_mode_detours_selected_filament_above_and_below_patch(self):
        gcode = b"""; CHANGE_LAYER
; OBJECT_ID: 1
T1
; FEATURE: Outer wall
G1 X1 Y0 E1
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X1 Y0.5 E1
; CHANGE_LAYER
T0
G0 X0 Y0
; FEATURE: Outer wall
G1 X1 Y0 E1
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X1 Y0.5 E1
; CHANGE_LAYER
T1
G0 X0 Y0
; FEATURE: Outer wall
G1 X1 Y0 E1
G0 X0 Y0.5
; FEATURE: Inner wall
G1 X1 Y0.5 E1
"""
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.gcode.3mf"
            with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("Metadata/plate_1.gcode", gcode)
            replacements = build_interval_detours(
                source,
                detour_side="both",
                subject_filaments={1},
                same_layer_gap_mm=0,
            )

        self.assertEqual(set(replacements), {5, 21})
        self.assertTrue(all(b"tapered detour in" in value for value in replacements.values()))


class ProgressMetadataTests(unittest.TestCase):
    def test_disables_prime_tower_in_project_metadata(self):
        result = json.loads(_rewrite_project_settings(json.dumps({
            "enable_prime_tower": "1",
            "filament_minimal_purge_on_wipe_tower": ["15", "15"],
            "keep": "yes",
        }).encode()))
        self.assertEqual(result["enable_prime_tower"], "0")
        self.assertEqual(result["filament_minimal_purge_on_wipe_tower"], ["0", "0"])
        self.assertEqual(result["keep"], "yes")

    def test_collapses_project_filament_arrays_to_one_virtual_filament(self):
        result = json.loads(_rewrite_project_settings(json.dumps({
            "filament_colour": ["#111", "#222", "#333"],
            "filament_type": ["PLA", "PETG", "TPU"],
            "filament_flow_ratio": [".98", ".99", ".97", ".96", ".95", ".94"],
            "flush_volumes_matrix": list(range(9)),
            "different_settings_to_system": ["process", "f1", "f2", "f3", "printer"],
            "unrelated_pair": ["left", "right"],
        }).encode()))
        self.assertEqual(result["filament_colour"], ["#111"])
        self.assertEqual(result["filament_type"], ["PLA"])
        self.assertEqual(result["filament_flow_ratio"], [".98", ".99"])
        self.assertEqual(result["flush_volumes_matrix"], [0])
        self.assertEqual(result["different_settings_to_system"], ["process", "f1", "printer"])
        self.assertEqual(result["unrelated_pair"], ["left", "right"])

    def test_collapses_plate_and_sequence_mapping_metadata(self):
        model = _rewrite_model_settings(b'''<?xml version="1.0"?><config><plate>
            <metadata key="filament_maps" value="1 1 1"/>
            <metadata key="filament_volume_maps" value="0 0 0"/>
        </plate></config>''').decode()
        self.assertIn('key="filament_maps" value="1"', model)
        self.assertIn('key="filament_volume_maps" value="0"', model)
        sequence = json.loads(_rewrite_sequence(json.dumps({"plate_1": {
            "sequence": [1, 2, 1], "nozzle_sequence": [0, 1, 0],
            "optimal_assignment": [0, 1, 2],
        }}).encode()))
        self.assertEqual(sequence["plate_1"]["sequence"], [1, 1, 1])
        self.assertEqual(sequence["plate_1"]["nozzle_sequence"], [0, 0, 0])
        self.assertEqual(sequence["plate_1"]["optimal_assignment"], [0])

    def test_rewrites_progress_from_layers_and_removes_stale_remaining_time(self):
        source = io.BytesIO(
            b"; model printing time: 2h; total estimated time: 3h\n"
            b"; filament_map = 1,1,1\n"
            b"; filament_nozzle_map = 0,0,0\n"
            b"; filament_colour = #111111;#222222;#333333\n"
            b"; filament_type = PLA;PETG;TPU\n"
            b"M73 P0 R180\nM73 L1\nM73 P40 R120\n"
            b"M73 L2\nM73 P90 R10\nM73 P100 R0\n"
        )
        destination = io.BytesIO()
        _rewrite_gcode_metadata(source, destination, CollapseReport())
        result = destination.getvalue()
        self.assertIn(b"M73 P0 R60\n", result)
        self.assertIn(b"M73 P50 R1\n", result)
        self.assertIn(b"M73 P100 R0\n", result)
        self.assertNotIn(b"R180", result)
        self.assertNotIn(b"model printing time: 2h", result)
        self.assertIn(b"; filament_map = 1\n", result)
        self.assertIn(b"; filament_nozzle_map = 0\n", result)
        self.assertIn(b"; filament_colour = #111111\n", result)
        self.assertIn(b"; filament_type = PLA\n", result)

    def test_collapses_initial_ams_selection_to_first_filament(self):
        source = io.BytesIO(
            b"M620 M\n"
            b"M620 S1A   ; switch material if AMS exists\n"
            b"    T1\n"
            b"M621 S1A\n"
            b"M620.1 E F548.788 T240\n"
            b"T1000\n"
            b"T255\n"
        )
        destination = io.BytesIO()
        _rewrite_gcode_metadata(source, destination, CollapseReport())
        result = destination.getvalue()
        self.assertIn(b"M620 S0A   ; switch material if AMS exists\n", result)
        self.assertIn(b"    T0\n", result)
        self.assertIn(b"M621 S0A\n", result)
        self.assertIn(b"M620.1 E F548.788 T240\n", result)
        self.assertIn(b"T1000\n", result)
        self.assertIn(b"T255\n", result)
        self.assertNotIn(b"M620 S1A", result)
        self.assertNotIn(b"M621 S1A", result)

    def test_plate_json_uses_zero_based_single_extruder(self):
        result = json.loads(_rewrite_plate_json(json.dumps({
            "filament_colors": ["#111", "#222"],
            "filament_ids": [0, 1],
            "first_extruder": 1,
        }).encode()))
        self.assertEqual(result["filament_colors"], ["#111"])
        self.assertEqual(result["filament_ids"], [0])
        self.assertEqual(result["first_extruder"], 0)


class FilenameTests(unittest.TestCase):
    def test_removes_repeated_multiword_plate_name(self):
        name = "RelicRush_A_V17(Bridge Tower Slice - 0.16)-prepared-fuzzy_Bridge Tower.gcode.3mf"
        self.assertEqual(
            clean_sliced_output_base(name),
            "RelicRush_A_V17(Bridge Tower Slice - 0.16)-prepared-fuzzy",
        )

    def test_keeps_unique_or_single_word_suffix(self):
        self.assertEqual(clean_sliced_output_base("Project_Bridge Tower.gcode.3mf"), "Project_Bridge Tower")
        self.assertEqual(clean_sliced_output_base("Project_part.gcode.3mf"), "Project_part")

    def test_next_available_destination_returns_path_unchanged_when_free(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Tower (FP-Print_Ready).gcode.3mf"
            self.assertEqual(next_available_destination(path), path)

    def test_next_available_destination_numbers_plainly_from_v2(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Tower (FP-Print_Ready).gcode.3mf"
            path.write_text("first run")
            self.assertEqual(
                next_available_destination(path).name,
                "Tower (FP-Print_Ready) v2.gcode.3mf",
            )
            (Path(folder) / "Tower (FP-Print_Ready) v2.gcode.3mf").write_text("second run")
            self.assertEqual(
                next_available_destination(path).name,
                "Tower (FP-Print_Ready) v3.gcode.3mf",
            )

    def test_next_available_destination_does_not_stack_version_suffixes(self):
        with tempfile.TemporaryDirectory() as folder:
            existing = Path(folder) / "Tower (FP-Print_Ready) v2.gcode.3mf"
            existing.write_text("already v2")
            self.assertEqual(
                next_available_destination(existing).name,
                "Tower (FP-Print_Ready) v3.gcode.3mf",
            )

    def test_next_available_destination_handles_plain_3mf_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Tower (FP-Prepped).3mf"
            path.write_text("first run")
            self.assertEqual(next_available_destination(path).name, "Tower (FP-Prepped) v2.3mf")

    def test_prepared_name_does_not_repeat_suffix(self):
        self.assertEqual(suggest_prepared_project_name("Tower.3mf"), "Tower (FP-Prepped).3mf")
        self.assertEqual(suggest_prepared_project_name("Tower-prepared.3mf"), "Tower (FP-Prepped).3mf")
        self.assertEqual(suggest_prepared_project_name("Tower (FP-Prepped).3mf"), "Tower (FP-Prepped v2).3mf")

    def test_prepared_name_also_recognises_the_old_c2f_suffix(self):
        # Re-running FuzzyPaint on a file named by an older version (back when
        # the tool was called Colour2Fuzz) must still strip that old suffix
        # instead of appending a second, unrelated one.
        self.assertEqual(suggest_prepared_project_name("Tower (C2F-Prepped).3mf"), "Tower (FP-Prepped v2).3mf")

    def test_three_stage_names(self):
        self.assertEqual(
            suggest_created_package_name("Tower (FP-Prepped).3mf"),
            "Tower (FP-Created).gcode.3mf",
        )
        self.assertEqual(
            suggest_print_package_name("Tower (FP-Created)_Bridge Tower.gcode.3mf"),
            "Tower (FP-Print_Ready).gcode.3mf",
        )
        self.assertEqual(
            suggest_print_package_name("Tower-prepared_Bridge Tower.gcode.3mf"),
            "Tower (FP-Print_Ready).gcode.3mf",
        )

    def test_three_stage_names_also_recognise_the_old_c2f_suffix(self):
        self.assertEqual(
            suggest_created_package_name("Tower (C2F-Prepped).3mf"),
            "Tower (FP-Created).gcode.3mf",
        )
        self.assertEqual(
            suggest_print_package_name("Tower (C2F-Created)_Bridge Tower.gcode.3mf"),
            "Tower (FP-Print_Ready).gcode.3mf",
        )


if __name__ == "__main__":
    unittest.main()
