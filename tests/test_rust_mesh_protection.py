import unittest

from fuzzypaint import (
    _find_flat_contact_protection,
    _find_fuzz_envelope_protection,
    _find_planar_protection,
    _parse_mesh_geometry,
)

try:
    import fuzzypaint_engine
except ImportError:
    fuzzypaint_engine = None


RUST_AVAILABLE = bool(
    fuzzypaint_engine
    and hasattr(fuzzypaint_engine, "find_flat_contact_and_fuzz_envelope")
    and hasattr(fuzzypaint_engine, "find_mesh_protection")
)


@unittest.skipUnless(RUST_AVAILABLE, "Rust mesh protection extension is not installed")
class RustMeshProtectionParityTests(unittest.TestCase):
    def assert_planar_parity(self, xml: bytes, *, minimum_area: float = 5.0):
        geometry = _parse_mesh_geometry(xml, {2})
        py_indices, py_count, py_area = _find_planar_protection(
            xml,
            {2},
            angle_degrees=2.0,
            minimum_area_mm2=minimum_area,
            compact_strip_minimum_area_mm2=None,
            protect_connected_coplanar=True,
            connected_coplanar_angle_degrees=2.0,
            connected_coplanar_plane_tolerance_mm=0.25,
            connected_coplanar_minimum_short_span_mm=1.0,
            connected_coplanar_minimum_long_span_mm=2.0,
            connected_coplanar_satellite_gap_mm=0.5,
            geometry=geometry,
        )
        rust_indices, rust_count, rust_area, _contacts, _envelope = (
            fuzzypaint_engine.find_mesh_protection(
                xml,
                [2],
                angle_degrees=2.0,
                minimum_area_mm2=minimum_area,
                compact_strip_minimum_area_mm2=None,
                protect_connected_coplanar=True,
                connected_coplanar_angle_degrees=2.0,
                connected_coplanar_plane_tolerance_mm=0.25,
                connected_coplanar_minimum_short_span_mm=1.0,
                connected_coplanar_minimum_long_span_mm=2.0,
                connected_coplanar_satellite_gap_mm=0.5,
            )
        )
        self.assertEqual(set(rust_indices), py_indices)
        self.assertEqual(rust_count, py_count)
        self.assertAlmostEqual(rust_area, py_area, places=10)

    def assert_parity(self, xml: bytes, *, rings: int = 1, distance: float = 0.25):
        geometry = _parse_mesh_geometry(xml, {2})
        py_contact_indices, py_contact_areas = _find_flat_contact_protection(
            xml, {2}, geometry=geometry
        )
        py_envelope_indices, py_envelope_areas = _find_fuzz_envelope_protection(
            xml,
            {2},
            distance_mm=distance,
            contact_seed_indices=py_contact_indices,
            contact_neighbor_rings=rings,
            geometry=geometry,
        )
        rust_contact, rust_envelope = (
            fuzzypaint_engine.find_flat_contact_and_fuzz_envelope(
                xml, [2], distance, rings
            )
        )
        rust_contact_areas = dict(rust_contact)
        rust_envelope_areas = dict(rust_envelope)
        self.assertEqual(set(rust_contact_areas), py_contact_indices)
        self.assertEqual(set(rust_envelope_areas), py_envelope_indices)
        for index, area in py_contact_areas.items():
            self.assertAlmostEqual(rust_contact_areas[index], area, places=10)
        for index, area in py_envelope_areas.items():
            self.assertAlmostEqual(rust_envelope_areas[index], area, places=10)

    def test_overlapping_face_footprint(self):
        self.assert_parity(b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/>
          <vertex x="0" y="10" z="0"/>
          <vertex x="2" y="2" z="0.02"/><vertex x="4" y="2" z="0.02"/>
          <vertex x="2" y="4" z="0.02"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>''')

    def test_shared_edge_is_not_contact(self):
        self.assert_parity(b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="2" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2"/>
        </triangles></mesh></object>''')

    def test_parallel_faces_outside_contact_gap(self):
        self.assert_parity(b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="4" y="0" z="0"/>
          <vertex x="0" y="4" z="0"/>
          <vertex x="1" y="1" z="0.10"/><vertex x="2" y="1" z="0.10"/>
          <vertex x="1" y="2" z="0.10"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="3" v2="4" v3="5"/>
        </triangles></mesh></object>''')

    def test_contact_seed_and_angled_envelope_neighbor(self):
        self.assert_parity(b'''<object id="1"><mesh><vertices>
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
        </triangles></mesh></object>''')

    def test_unobstructed_face(self):
        self.assert_parity(b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
        </triangles></mesh></object>''')

    def test_planar_large_patch_and_small_island(self):
        self.assert_planar_parity(b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="4" y="0" z="0"/>
          <vertex x="0" y="3" z="0"/><vertex x="4" y="3" z="0"/>
          <vertex x="10" y="0" z="0"/><vertex x="10.5" y="0" z="0"/>
          <vertex x="10" y="0.5" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
        </triangles></mesh></object>''')

    def test_planar_connected_component_and_nearby_satellite(self):
        self.assert_planar_parity(b'''<object id="1"><mesh><vertices>
          <vertex x="0" y="0" z="0"/><vertex x="3" y="0" z="0"/>
          <vertex x="0" y="2" z="0"/><vertex x="3" y="2" z="0"/>
          <vertex x="3.2" y="0.5" z="0"/><vertex x="3.5" y="0.5" z="0"/>
          <vertex x="3.2" y="0.8" z="0"/>
        </vertices><triangles>
          <triangle v1="0" v2="1" v3="2" paint_color="8"/>
          <triangle v1="1" v2="3" v3="2" paint_color="8"/>
          <triangle v1="4" v2="5" v3="6" paint_color="8"/>
        </triangles></mesh></object>''', minimum_area=100.0)


if __name__ == "__main__":
    unittest.main()
