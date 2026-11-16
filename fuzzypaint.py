"""Convert Bambu Studio colour-paint masks into fuzzy-skin paint masks.

Bambu Studio stores both kinds of paint as TriangleSelector nibble streams on
individual <triangle> elements inside a 3MF (ZIP) package.  This module maps
the leaf states in ``paint_color`` to the single fuzzy-skin state while keeping
the subdivision tree intact.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import tempfile
import zipfile
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple

try:
    import fuzzypaint_engine as _rust_engine
except ImportError:
    _rust_engine = None
else:
    if not hasattr(_rust_engine, "find_flat_contact_and_fuzz_envelope"):
        _rust_engine = None


class ConversionError(ValueError):
    """Raised when a project or paint stream cannot be converted safely."""


@dataclass(frozen=True)
class PaintNode:
    state: Optional[int] = None
    split_code: Optional[int] = None
    children: Tuple["PaintNode", ...] = ()

    @property
    def is_leaf(self) -> bool:
        return self.state is not None


def decode_paint_stream(value: str) -> PaintNode:
    """Decode one Bambu/Prusa TriangleSelector hexadecimal string."""
    if not value or not re.fullmatch(r"[0-9a-fA-F]+", value):
        raise ConversionError(f"Invalid paint stream: {value!r}")

    # FacetsAnnotation writes each new nibble at the front of the string.
    nibbles = [int(ch, 16) for ch in reversed(value)]
    position = 0

    def take() -> int:
        nonlocal position
        if position >= len(nibbles):
            raise ConversionError(f"Truncated paint stream: {value!r}")
        result = nibbles[position]
        position += 1
        return result

    def parse() -> PaintNode:
        code = take()
        split_sides = code & 0b11
        if split_sides:
            # Children are already in the on-disk (reverse compatibility) order.
            children = tuple(parse() for _ in range(split_sides + 1))
            return PaintNode(split_code=code, children=children)

        state_marker = code >> 2
        if state_marker < 3:
            return PaintNode(state=state_marker)

        state = 3
        extension = take()
        while extension == 0xF:
            state += 15
            extension = take()
        return PaintNode(state=state + extension)

    root = parse()
    if position != len(nibbles):
        raise ConversionError(
            f"Paint stream has {len(nibbles) - position} unexpected trailing nibble(s): {value!r}"
        )
    return root


def encode_paint_stream(node: PaintNode) -> str:
    """Encode a paint tree in Bambu's reversed-nibble representation."""
    nibbles = []

    def emit(current: PaintNode) -> None:
        if current.is_leaf:
            state = int(current.state or 0)
            if state < 0:
                raise ConversionError("Paint states cannot be negative")
            if state < 3:
                nibbles.append(state << 2)
            else:
                nibbles.append(0xC)
                remainder = state - 3
                while remainder >= 15:
                    nibbles.append(0xF)
                    remainder -= 15
                nibbles.append(remainder)
            return

        if current.split_code is None:
            raise ConversionError("A split paint node is missing its split code")
        expected = (current.split_code & 0b11) + 1
        if not (1 <= (current.split_code & 0b11) <= 3) or len(current.children) != expected:
            raise ConversionError("Invalid split paint node")
        nibbles.append(current.split_code)
        for child in current.children:
            emit(child)

    emit(node)
    return "".join(format(nibble, "x") for nibble in reversed(nibbles))


def leaf_states(node: PaintNode) -> Iterable[int]:
    if node.is_leaf:
        yield int(node.state or 0)
    else:
        for child in node.children:
            yield from leaf_states(child)


def colour_tree_to_fuzzy(node: PaintNode, slots: Optional[Set[int]]) -> PaintNode:
    """Map selected non-zero colour leaves to fuzzy state 1, others to 0."""
    if node.is_leaf:
        state = int(node.state or 0)
        selected = state > 0 and (slots is None or state in slots)
        return PaintNode(state=1 if selected else 0)
    return PaintNode(
        split_code=node.split_code,
        children=tuple(colour_tree_to_fuzzy(child, slots) for child in node.children),
    )


def remove_selected_colours(node: PaintNode, slots: Optional[Set[int]]) -> PaintNode:
    """Erase selected colour leaves while retaining unselected slot painting."""
    if node.is_leaf:
        state = int(node.state or 0)
        selected = state > 0 and (slots is None or state in slots)
        return PaintNode(state=0 if selected else state)
    return PaintNode(
        split_code=node.split_code,
        children=tuple(remove_selected_colours(child, slots) for child in node.children),
    )


@dataclass
class ConversionReport:
    archive_entries: int = 0
    model_files: int = 0
    model_files_modified: int = 0
    triangles_scanned: int = 0
    colour_triangles: int = 0
    fuzzy_triangles_written: int = 0
    colour_attributes_removed: int = 0
    colour_attributes_updated: int = 0
    skipped_colour_triangles: int = 0
    planar_patches_protected: int = 0
    planar_triangles_protected: int = 0
    planar_area_protected_mm2: float = 0.0
    overlap_triangles_protected: int = 0
    overlap_area_protected_mm2: float = 0.0
    flat_contact_triangles_protected: int = 0
    flat_contact_area_protected_mm2: float = 0.0
    fuzz_envelope_triangles_protected: int = 0
    fuzz_envelope_area_protected_mm2: float = 0.0
    coincident_triangles_protected: int = 0
    coincident_area_protected_mm2: float = 0.0
    nonmanifold_triangles_protected: int = 0
    nonmanifold_area_protected_mm2: float = 0.0
    single_colour_attributes_removed: int = 0
    single_colour_assignments_remapped: int = 0
    single_spool_pairs_zeroed: int = 0
    wipe_while_retracting_disabled: bool = False
    retractions_disabled: bool = False
    sloped_z_hop_enabled: bool = False
    attachments_removed: int = 0
    attachment_bytes_removed: int = 0
    selected_leaf_counts: Counter = field(default_factory=Counter)


TRIANGLE_RE = re.compile(br"<(?:[A-Za-z_][\w.-]*:)?triangle\b[^>]*?/?>", re.IGNORECASE)
VERTEX_RE = re.compile(br"<(?:[A-Za-z_][\w.-]*:)?vertex\b[^>]*?/?>", re.IGNORECASE)
OBJECT_RE = re.compile(
    br"<(?:[A-Za-z_][\w.-]*:)?object\b[^>]*>.*?</(?:[A-Za-z_][\w.-]*:)?object\s*>",
    re.IGNORECASE | re.DOTALL,
)


def _attribute_pattern(name: bytes) -> re.Pattern:
    return re.compile(
        rb"(?P<space>\s+)" + re.escape(name) + rb"\s*=\s*(?P<quote>[\"'])(?P<value>.*?)(?P=quote)",
        re.IGNORECASE,
    )


COLOR_ATTR_RE = _attribute_pattern(b"paint_color")
FUZZY_ATTR_RE = _attribute_pattern(b"paint_fuzzy_skin")
_SIMPLE_ATTRIBUTE_PATTERNS = {
    name: re.compile(
        rb"(?:^|\s)" + name + rb"\s*=\s*([\"'])(.*?)\1",
        re.IGNORECASE,
    )
    for name in (b"x", b"y", b"z", b"v1", b"v2", b"v3")
}


def _strip_all_colour_paint(data: bytes) -> Tuple[bytes, int]:
    """Remove every per-face colour annotation while retaining other paint channels."""
    return COLOR_ATTR_RE.subn(b"", data)


def _remap_extruder_metadata(data: bytes, slot: int) -> Tuple[bytes, int]:
    """Set every object/part extruder assignment in model_settings.config."""
    metadata_re = re.compile(
        rb"<(?:[A-Za-z_][\w.-]*:)?metadata\b(?=[^>]*\bkey\s*=\s*([\"'])extruder\1)[^>]*?/?>",
        re.IGNORECASE,
    )
    value_re = re.compile(rb"(\bvalue\s*=\s*)([\"'])(.*?)\2", re.IGNORECASE)
    changed = 0

    def replace_metadata(match: re.Match) -> bytes:
        nonlocal changed
        tag = match.group(0)
        value_match = value_re.search(tag)
        if not value_match or value_match.group(3) == str(slot).encode("ascii"):
            return tag
        changed += 1
        start, end = value_match.span(3)
        return tag[:start] + str(slot).encode("ascii") + tag[end:]

    return metadata_re.sub(replace_metadata, data), changed


def _configure_single_spool_segmentation(
    data: bytes, slots: Set[int], base_slot: int = 1
) -> Tuple[bytes, int]:
    """Keep virtual colour regions but remove purge/tower costs between same-spool slots."""
    try:
        settings = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("Could not read project_settings.config") from exc

    matrix = settings.get("flush_volumes_matrix")
    if not isinstance(matrix, list):
        raise ConversionError("Project settings contain no flushing-volume matrix")
    side = math.isqrt(len(matrix))
    if side * side != len(matrix):
        raise ConversionError("Project flushing-volume matrix is not square")

    involved = {base_slot, *slots}
    if any(slot < 1 or slot > side for slot in involved):
        raise ConversionError(
            f"Single-spool slot selection exceeds the project's {side} filament positions"
        )

    changed_pairs = 0
    for slot in slots:
        if slot == base_slot:
            continue
        for source, target in ((base_slot, slot), (slot, base_slot)):
            index = (source - 1) * side + (target - 1)
            if str(matrix[index]) != "0":
                matrix[index] = "0"
                changed_pairs += 1

    settings["enable_prime_tower"] = "0"
    minimal = settings.get("filament_minimal_purge_on_wipe_tower")
    if isinstance(minimal, list):
        for slot in involved:
            if slot <= len(minimal):
                minimal[slot - 1] = "0"
    vector = settings.get("flush_volumes_vector")
    if isinstance(vector, list) and len(vector) >= side * 2:
        for slot in involved:
            vector[(slot - 1) * 2] = "0"
            vector[(slot - 1) * 2 + 1] = "0"

    encoded = json.dumps(settings, ensure_ascii=False, indent=4).encode("utf-8") + b"\n"
    return encoded, changed_pairs


def _simple_attribute(tag: bytes, name: bytes) -> Optional[bytes]:
    pattern = _SIMPLE_ATTRIBUTE_PATTERNS.get(name)
    if pattern is None:
        pattern = re.compile(
            rb"(?:^|\s)" + re.escape(name) + rb"\s*=\s*([\"'])(.*?)\1",
            re.IGNORECASE,
        )
    match = pattern.search(tag)
    return match.group(2) if match else None


def _selected_colour_in_tag(tag: bytes, slots: Optional[Set[int]]) -> bool:
    match = COLOR_ATTR_RE.search(tag)
    if not match:
        return False
    tree = decode_paint_stream(match.group("value").decode("ascii"))
    return any(state > 0 and (slots is None or state in slots) for state in leaf_states(tree))


@dataclass(frozen=True)
class _MeshGeometry:
    """Geometry shared by protection passes for one 3MF object."""

    vertices: List[Tuple[float, float, float]]
    source_indices: List[int]
    vertex_indices: List[Tuple[int, int, int]]
    triangle_points: List[Tuple[Tuple[float, float, float], ...]]
    areas: List[float]
    normals: List[Tuple[float, float, float]]
    bounds: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]
    selected: List[bool]


def _parse_mesh_geometry(
    object_xml: bytes, slots: Optional[Set[int]]
) -> Optional[_MeshGeometry]:
    """Parse reusable triangle geometry while preserving source XML indices."""
    vertices: List[Tuple[float, float, float]] = []
    for match in VERTEX_RE.finditer(object_xml):
        tag = match.group(0)
        coords = [_simple_attribute(tag, axis) for axis in (b"x", b"y", b"z")]
        if any(value is None for value in coords):
            return None
        vertices.append(tuple(float(value) for value in coords))
    if not vertices:
        return None

    source_indices: List[int] = []
    vertex_indices: List[Tuple[int, int, int]] = []
    triangle_points: List[Tuple[Tuple[float, float, float], ...]] = []
    areas: List[float] = []
    normals: List[Tuple[float, float, float]] = []
    bounds: List[Tuple[Tuple[float, float, float], Tuple[float, float, float]]] = []
    selected: List[bool] = []
    for triangle_index, match in enumerate(TRIANGLE_RE.finditer(object_xml)):
        tag = match.group(0)
        raw_indices = [_simple_attribute(tag, key) for key in (b"v1", b"v2", b"v3")]
        if any(value is None for value in raw_indices):
            continue
        indices = tuple(int(value) for value in raw_indices)
        if any(index < 0 or index >= len(vertices) for index in indices):
            continue
        points = tuple(vertices[index] for index in indices)
        a, b, c = points
        ab = tuple(b[axis] - a[axis] for axis in range(3))
        ac = tuple(c[axis] - a[axis] for axis in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in cross))
        if length <= 1e-12:
            continue
        source_indices.append(triangle_index)
        vertex_indices.append(indices)
        triangle_points.append(points)
        areas.append(length * 0.5)
        normals.append(tuple(value / length for value in cross))
        bounds.append(
            (
                tuple(min(point[axis] for point in points) for axis in range(3)),
                tuple(max(point[axis] for point in points) for axis in range(3)),
            )
        )
        selected.append(_selected_colour_in_tag(tag, slots))

    return _MeshGeometry(
        vertices, source_indices, vertex_indices, triangle_points,
        areas, normals, bounds, selected,
    )


def _find_planar_protection(
    object_xml: bytes,
    slots: Optional[Set[int]],
    *,
    angle_degrees: float,
    minimum_area_mm2: float,
    compact_strip_minimum_area_mm2: Optional[float] = None,
    compact_strip_minimum_aspect_ratio: float = 3.0,
    compact_strip_minimum_fill_ratio: float = 0.65,
    compact_strip_maximum_length_mm: float = 30.0,
    compact_strip_maximum_normal_z: float = 0.1,
    coplanar_lane_minimum_patch_area_mm2: Optional[float] = None,
    coplanar_lane_maximum_plane_gap_mm: float = 0.15,
    coplanar_lane_maximum_side_gap_mm: float = 0.75,
    coplanar_lane_maximum_forward_gap_mm: float = 15.0,
    coplanar_lane_minimum_aspect_ratio: float = 3.0,
    coplanar_lane_minimum_fill_ratio: float = 0.4,
    coplanar_lane_maximum_length_mm: float = 60.0,
    coplanar_lane_axis: Optional[
        Tuple[float, float, float] | Sequence[Tuple[float, float, float]]
    ] = None,
    coplanar_lane_axis_minimum_fill_ratio: float = 0.3,
    layered_feature_maximum_depth_mm: Optional[float] = None,
    protect_connected_coplanar: bool = False,
    connected_coplanar_angle_degrees: float = 0.5,
    connected_coplanar_plane_tolerance_mm: float = 0.02,
    connected_coplanar_minimum_short_span_mm: float = 0.0,
    connected_coplanar_minimum_long_span_mm: float = 0.0,
    connected_coplanar_satellite_gap_mm: float = 0.0,
    geometry: Optional[_MeshGeometry] = None,
) -> Tuple[Set[int], int, float]:
    """Find selected painted triangles belonging to large coplanar patches.

    Connectivity between triangles is keyed on each edge's *vertex positions*
    (rounded to tolerate float noise), not on raw vertex indices. Exporters
    routinely duplicate a vertex that sits on a UV/material/hard-normal seam,
    so two triangles that are geometrically edge-adjacent can reference two
    different index numbers for what is the same physical point -- matching
    by index alone silently fragments one large flat patch into many small
    disconnected components, each of which can then fall under the minimum-
    area threshold and get fuzzed even though the whole patch should have
    been protected.

    A patch grows by comparing each candidate triangle to the patch's own
    cumulative area-weighted average normal, not just to the immediate
    neighbour that discovered it. Real-world meshes often carry small-scale
    modelling noise (bumps baked into the mesh, not fuzz) where two adjacent
    triangles can individually differ by more than the angle tolerance even
    though the surface reads as flat to a human eye; comparing only to one
    neighbour at a time lets that local noise fragment an otherwise flat
    patch into sub-threshold pieces that each get fuzzed. Comparing to the
    patch's running average instead lets gradual, noise-level drift average
    out across the whole patch, while a genuine corner or fold (a real
    normal change, not noise) still fails the comparison and stops the
    patch growing through it.

    Patch discovery deliberately considers *all* valid triangles, not only
    triangles carrying one of the selected paint colours. Paint boundaries
    are not geometric boundaries: a large flat face remains a large flat
    face when only part of it is painted. After the geometric patches have
    been classified by normal and total area, only selected painted
    triangles inside qualifying patches are returned for protection.

    Qualifying patches do not absorb neighbouring angle-separated patches.
    Doing that recursively turns ordinary edge connectivity into an
    effectively unbounded flood fill: one large flat seed can eventually
    protect an entire connected painted model, including curved and angled
    details. Every protected patch must therefore meet the flatness and area
    requirements on its own.
    """
    geometry = geometry or _parse_mesh_geometry(object_xml, slots)
    if geometry is None:
        return set(), 0, 0.0
    vertices = geometry.vertices
    areas = geometry.areas
    normals = geometry.normals
    source_indices = geometry.source_indices
    vertex_indices = geometry.vertex_indices
    selected = geometry.selected

    def position_key(index: int) -> Tuple[float, float, float]:
        return tuple(round(value, 4) for value in vertices[index])

    adjacency: List[List[int]] = [[] for _ in areas]
    edge_owner = {}
    cosine_limit = math.cos(math.radians(angle_degrees))

    for candidate, indices in enumerate(vertex_indices):
        for edge in (
            tuple(sorted((position_key(indices[0]), position_key(indices[1])))),
            tuple(sorted((position_key(indices[1]), position_key(indices[2])))),
            tuple(sorted((position_key(indices[2]), position_key(indices[0])))),
        ):
            neighbor = edge_owner.get(edge)
            if neighbor is None:
                edge_owner[edge] = candidate
                continue
            adjacency[neighbor].append(candidate)
            adjacency[candidate].append(neighbor)

    # Region-grow each patch, comparing every candidate to the patch's own
    # cumulative area-weighted average normal (recomputed before each check,
    # so it reflects every member merged so far) rather than to whichever
    # single neighbour discovered it -- see the docstring for why.
    visited = [False] * len(areas)
    patches: List[Tuple[List[int], float, Tuple[float, float, float]]] = []
    for start in range(len(areas)):
        if visited[start]:
            continue
        visited[start] = True
        members = [start]
        sum_normal = [normals[start][axis] * areas[start] for axis in range(3)]
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if visited[neighbor]:
                    continue
                length = math.sqrt(sum(value * value for value in sum_normal))
                if length <= 1e-12:
                    continue
                reference = tuple(value / length for value in sum_normal)
                dot = sum(reference[axis] * normals[neighbor][axis] for axis in range(3))
                if dot < cosine_limit:
                    continue
                visited[neighbor] = True
                members.append(neighbor)
                queue.append(neighbor)
                for axis in range(3):
                    sum_normal[axis] += normals[neighbor][axis] * areas[neighbor]
        patches.append(
            (members, sum(areas[member] for member in members), tuple(sum_normal))
        )

    def compact_vertical_strip(
        members: List[int], total_area: float, sum_normal: Tuple[float, float, float]
    ) -> bool:
        """Recognise small, dense vertical trapezoids such as recessed channel walls.

        This is deliberately opt-in. A low global area threshold catches many
        broad but sparse coplanar fragments scattered across decorative rock.
        Functional channel walls instead occupy most of a compact, elongated
        vertical bounding rectangle. The fill/aspect/length constraints keep
        this exception narrow and leave irregular textured regions eligible
        for fuzz.
        """
        if compact_strip_minimum_area_mm2 is None:
            return False
        if total_area < compact_strip_minimum_area_mm2:
            return False
        normal_length = math.sqrt(sum(value * value for value in sum_normal))
        if normal_length <= 1e-12:
            return False
        normal = tuple(value / normal_length for value in sum_normal)
        if abs(normal[2]) > compact_strip_maximum_normal_z:
            return False

        horizontal_length = math.hypot(normal[0], normal[1])
        if horizontal_length <= 1e-12:
            return False
        horizontal = (-normal[1] / horizontal_length, normal[0] / horizontal_length, 0.0)
        vertical = (
            normal[1] * horizontal[2] - normal[2] * horizontal[1],
            normal[2] * horizontal[0] - normal[0] * horizontal[2],
            normal[0] * horizontal[1] - normal[1] * horizontal[0],
        )

        horizontal_values = []
        vertical_values = []
        for member in members:
            for vertex_index in vertex_indices[member]:
                point = vertices[vertex_index]
                horizontal_values.append(
                    sum(point[axis] * horizontal[axis] for axis in range(3))
                )
                vertical_values.append(
                    sum(point[axis] * vertical[axis] for axis in range(3))
                )
        horizontal_span = max(horizontal_values) - min(horizontal_values)
        vertical_span = max(vertical_values) - min(vertical_values)
        shorter = min(horizontal_span, vertical_span)
        longer = max(horizontal_span, vertical_span)
        if shorter <= 1e-9 or longer > compact_strip_maximum_length_mm:
            return False
        if longer / shorter < compact_strip_minimum_aspect_ratio:
            return False
        return (
            total_area / (horizontal_span * vertical_span)
            >= compact_strip_minimum_fill_ratio
        )

    def canonical_normal(sum_normal: Tuple[float, float, float]) -> Tuple[float, float, float]:
        length = math.sqrt(sum(value * value for value in sum_normal))
        normal = tuple(value / length for value in sum_normal)
        dominant = max(range(3), key=lambda axis: abs(normal[axis]))
        return tuple(-value for value in normal) if normal[dominant] < 0 else normal

    def plane_basis(
        normal: Tuple[float, float, float]
    ) -> Tuple[Tuple[float, float, float], Tuple[float, float, float]]:
        reference_axis = min(range(3), key=lambda axis: abs(normal[axis]))
        reference = tuple(1.0 if axis == reference_axis else 0.0 for axis in range(3))
        first = (
            normal[1] * reference[2] - normal[2] * reference[1],
            normal[2] * reference[0] - normal[0] * reference[2],
            normal[0] * reference[1] - normal[1] * reference[0],
        )
        first_length = math.sqrt(sum(value * value for value in first))
        first = tuple(value / first_length for value in first)
        second = (
            normal[1] * first[2] - normal[2] * first[1],
            normal[2] * first[0] - normal[0] * first[2],
            normal[0] * first[1] - normal[1] * first[0],
        )
        return first, second

    def project(
        points: List[Tuple[float, float, float]], axis: Tuple[float, float, float]
    ) -> List[float]:
        return [sum(point[index] * axis[index] for index in range(3)) for point in points]

    lane_patch_indices: Set[int] = set()
    layered_patch_indices: Set[int] = set()
    if coplanar_lane_minimum_patch_area_mm2 is not None:
        descriptors = []
        for patch_index, (members, total_area, sum_normal) in enumerate(patches):
            if not (coplanar_lane_minimum_patch_area_mm2 <= total_area < minimum_area_mm2):
                continue
            if not any(selected[member] for member in members):
                continue
            unique_vertices = sorted(
                {vertex for member in members for vertex in vertex_indices[member]}
            )
            points = [vertices[index] for index in unique_vertices]
            normal = canonical_normal(sum_normal)
            first, second = plane_basis(normal)
            first_values = project(points, first)
            second_values = project(points, second)
            first_span = max(first_values) - min(first_values)
            second_span = max(second_values) - min(second_values)
            rectangle_area = first_span * second_span
            if rectangle_area <= 1e-9 or total_area / rectangle_area < 0.4:
                continue
            center = tuple(
                sum(point[axis] for point in points) / len(points) for axis in range(3)
            )
            descriptors.append(
                {
                    "patch_index": patch_index,
                    "points": points,
                    "normal": normal,
                    "plane": sum(normal[axis] * center[axis] for axis in range(3)),
                    "area": total_area,
                }
            )

        parents = list(range(len(descriptors)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        def interval_gap(left_min: float, left_max: float, right_min: float, right_max: float) -> float:
            return max(0.0, left_min - right_max, right_min - left_max)

        for left_index, left in enumerate(descriptors):
            first, second = plane_basis(left["normal"])
            left_first = project(left["points"], first)
            left_second = project(left["points"], second)
            for right_index in range(left_index + 1, len(descriptors)):
                right = descriptors[right_index]
                dot = sum(left["normal"][axis] * right["normal"][axis] for axis in range(3))
                if dot < cosine_limit:
                    continue
                right_plane_values = project(right["points"], left["normal"])
                right_plane = sum(right_plane_values) / len(right_plane_values)
                if abs(left["plane"] - right_plane) > coplanar_lane_maximum_plane_gap_mm:
                    continue
                right_first = project(right["points"], first)
                right_second = project(right["points"], second)
                first_gap = interval_gap(
                    min(left_first), max(left_first), min(right_first), max(right_first)
                )
                second_gap = interval_gap(
                    min(left_second), max(left_second), min(right_second), max(right_second)
                )
                if (
                    min(first_gap, second_gap) <= coplanar_lane_maximum_side_gap_mm
                    and max(first_gap, second_gap) <= coplanar_lane_maximum_forward_gap_mm
                ):
                    union(left_index, right_index)

        groups = {}
        for descriptor_index, descriptor in enumerate(descriptors):
            groups.setdefault(find(descriptor_index), []).append(descriptor)
        for group in groups.values():
            total_area = sum(descriptor["area"] for descriptor in group)
            if total_area < minimum_area_mm2:
                continue
            normal = group[0]["normal"]
            first, second = plane_basis(normal)
            points = [point for descriptor in group for point in descriptor["points"]]
            coordinates = list(zip(project(points, first), project(points, second)))
            mean_first = sum(value[0] for value in coordinates) / len(coordinates)
            mean_second = sum(value[1] for value in coordinates) / len(coordinates)
            centered = [
                (value[0] - mean_first, value[1] - mean_second) for value in coordinates
            ]
            covariance_first = sum(value[0] * value[0] for value in centered) / len(centered)
            covariance_second = sum(value[1] * value[1] for value in centered) / len(centered)
            covariance_cross = sum(value[0] * value[1] for value in centered) / len(centered)
            angle = 0.5 * math.atan2(
                2.0 * covariance_cross, covariance_first - covariance_second
            )
            cosine = math.cos(angle)
            sine = math.sin(angle)
            aligned_first = [value[0] * cosine + value[1] * sine for value in centered]
            aligned_second = [-value[0] * sine + value[1] * cosine for value in centered]
            first_span = max(aligned_first) - min(aligned_first)
            second_span = max(aligned_second) - min(aligned_second)
            shorter = min(first_span, second_span)
            longer = max(first_span, second_span)
            if shorter <= 1e-9 or longer > coplanar_lane_maximum_length_mm:
                continue
            if longer / shorter < coplanar_lane_minimum_aspect_ratio:
                continue
            if total_area / (shorter * longer) < coplanar_lane_minimum_fill_ratio:
                continue
            lane_patch_indices.update(descriptor["patch_index"] for descriptor in group)

        # A caller that knows the object's local up-axis can opt into a more
        # selective directional pass. The generic graph above deliberately
        # has no preferred direction, but on repeated architectural channels
        # a horizontal row of coplanar panels can bridge several otherwise
        # separate vertical lanes into one broad group. Holding each seed's
        # narrow side-band fixed prevents that transitive sideways walk while
        # still allowing gaps along the requested lane axis.
        if coplanar_lane_axis is not None:
            first_axis_value = coplanar_lane_axis[0]
            if isinstance(first_axis_value, (int, float)):
                raw_axes = [coplanar_lane_axis]
            else:
                raw_axes = list(coplanar_lane_axis)
            for raw_axis_value in raw_axes:
                raw_axis = tuple(float(value) for value in raw_axis_value)
                if len(raw_axis) != 3:
                    raise ValueError("each coplanar lane axis must contain three values")
                raw_axis_length = math.sqrt(sum(value * value for value in raw_axis))
                if raw_axis_length <= 1e-12:
                    raise ValueError("coplanar lane axes must be non-zero vectors")
                raw_axis = tuple(value / raw_axis_length for value in raw_axis)
                directional_groups: Set[Tuple[int, ...]] = set()
                for seed_index, seed in enumerate(descriptors):
                    normal = seed["normal"]
                    projection = sum(raw_axis[axis] * normal[axis] for axis in range(3))
                    forward = tuple(
                        raw_axis[axis] - projection * normal[axis] for axis in range(3)
                    )
                    forward_length = math.sqrt(sum(value * value for value in forward))
                    if forward_length <= 1e-12:
                        continue
                    forward = tuple(value / forward_length for value in forward)
                    side = (
                        normal[1] * forward[2] - normal[2] * forward[1],
                        normal[2] * forward[0] - normal[0] * forward[2],
                        normal[0] * forward[1] - normal[1] * forward[0],
                    )
                    seed_side = project(seed["points"], side)
                    eligible = []
                    for descriptor_index, descriptor in enumerate(descriptors):
                        dot = sum(
                            normal[axis] * descriptor["normal"][axis] for axis in range(3)
                        )
                        if dot < cosine_limit:
                            continue
                        descriptor_plane_values = project(descriptor["points"], normal)
                        descriptor_plane = sum(descriptor_plane_values) / len(
                            descriptor_plane_values
                        )
                        if abs(seed["plane"] - descriptor_plane) > coplanar_lane_maximum_plane_gap_mm:
                            continue
                        descriptor_side = project(descriptor["points"], side)
                        if interval_gap(
                            min(seed_side),
                            max(seed_side),
                            min(descriptor_side),
                            max(descriptor_side),
                        ) > coplanar_lane_maximum_side_gap_mm:
                            continue
                        descriptor_forward = project(descriptor["points"], forward)
                        eligible.append(
                            (
                                descriptor_index,
                                min(descriptor_forward),
                                max(descriptor_forward),
                            )
                        )
                    if not any(index == seed_index for index, _minimum, _maximum in eligible):
                        continue
                    component = {seed_index}
                    changed = True
                    while changed:
                        changed = False
                        for descriptor_index, minimum, maximum in eligible:
                            if descriptor_index in component:
                                continue
                            for member_index in component:
                                member_forward = project(
                                    descriptors[member_index]["points"], forward
                                )
                                if interval_gap(
                                    minimum,
                                    maximum,
                                    min(member_forward),
                                    max(member_forward),
                                ) <= coplanar_lane_maximum_forward_gap_mm:
                                    component.add(descriptor_index)
                                    changed = True
                                    break
                    if len(component) > 1:
                        directional_groups.add(tuple(sorted(component)))

                for descriptor_indices in directional_groups:
                    group = [descriptors[index] for index in descriptor_indices]
                    total_area = sum(descriptor["area"] for descriptor in group)
                    if total_area < minimum_area_mm2:
                        continue
                    normal = group[0]["normal"]
                    projection = sum(raw_axis[axis] * normal[axis] for axis in range(3))
                    forward = tuple(
                        raw_axis[axis] - projection * normal[axis] for axis in range(3)
                    )
                    forward_length = math.sqrt(sum(value * value for value in forward))
                    if forward_length <= 1e-12:
                        continue
                    forward = tuple(value / forward_length for value in forward)
                    side = (
                        normal[1] * forward[2] - normal[2] * forward[1],
                        normal[2] * forward[0] - normal[0] * forward[2],
                        normal[0] * forward[1] - normal[1] * forward[0],
                    )
                    points = [point for descriptor in group for point in descriptor["points"]]
                    side_values = project(points, side)
                    forward_values = project(points, forward)
                    side_span = max(side_values) - min(side_values)
                    lane_length = max(forward_values) - min(forward_values)
                    if side_span <= 1e-9 or lane_length > coplanar_lane_maximum_length_mm:
                        continue
                    if lane_length / side_span < coplanar_lane_minimum_aspect_ratio:
                        continue
                    if total_area / (side_span * lane_length) < coplanar_lane_axis_minimum_fill_ratio:
                        continue
                    lane_patch_indices.update(
                        descriptor["patch_index"] for descriptor in group
                    )

        # Protect one directly adjacent small coplanar satellite of an
        # already-qualifying elongated, compact-strip, or lane patch. This is intentionally a
        # one-hop classification over the original patches: newly accepted
        # satellites never become hosts, so it cannot recreate the old
        # recursive absorption flood-fill.
        satellite_hosts = []
        for patch_index, (members, total_area, sum_normal) in enumerate(patches):
            if not any(selected[member] for member in members):
                continue
            unique_vertices = sorted(
                {vertex for member in members for vertex in vertex_indices[member]}
            )
            points = [vertices[index] for index in unique_vertices]
            normal = canonical_normal(sum_normal)
            first, second = plane_basis(normal)
            coordinates = list(zip(project(points, first), project(points, second)))
            mean_first = sum(value[0] for value in coordinates) / len(coordinates)
            mean_second = sum(value[1] for value in coordinates) / len(coordinates)
            centered = [
                (value[0] - mean_first, value[1] - mean_second) for value in coordinates
            ]
            covariance_first = sum(value[0] * value[0] for value in centered) / len(centered)
            covariance_second = sum(value[1] * value[1] for value in centered) / len(centered)
            covariance_cross = sum(value[0] * value[1] for value in centered) / len(centered)
            angle = 0.5 * math.atan2(
                2.0 * covariance_cross, covariance_first - covariance_second
            )
            cosine = math.cos(angle)
            sine = math.sin(angle)
            aligned_first = [value[0] * cosine + value[1] * sine for value in centered]
            aligned_second = [-value[0] * sine + value[1] * cosine for value in centered]
            first_span = max(aligned_first) - min(aligned_first)
            second_span = max(aligned_second) - min(aligned_second)
            shorter = min(first_span, second_span)
            longer = max(first_span, second_span)
            elongated_large = (
                total_area >= minimum_area_mm2
                and shorter > 1e-9
                and longer <= coplanar_lane_maximum_length_mm
                and longer / shorter >= coplanar_lane_minimum_aspect_ratio
                and total_area / (shorter * longer) >= coplanar_lane_minimum_fill_ratio
            )
            compact_host = compact_vertical_strip(members, total_area, sum_normal)
            lane_host = patch_index in lane_patch_indices
            if not (elongated_large or compact_host or lane_host):
                continue
            center = tuple(
                sum(point[axis] for point in points) / len(points) for axis in range(3)
            )
            satellite_hosts.append(
                {
                    "patch_index": patch_index,
                    "points": points,
                    "normal": normal,
                    "plane": sum(normal[axis] * center[axis] for axis in range(3)),
                }
            )

        satellite_candidates = []
        for patch_index, (members, total_area, sum_normal) in enumerate(patches):
            # Once a surface has independently qualified as a compact strip or
            # lane, admit even sub-threshold disconnected fragments that sit
            # directly beside it on the same plane.  Repair operations often
            # weld these slivers into their host; unrepaired exports leave them
            # as separate pieces.  This remains a one-hop classification:
            # admitted fragments never become hosts and cannot flood onward.
            if not (0.0 < total_area < minimum_area_mm2):
                continue
            if not any(selected[member] for member in members):
                continue
            unique_vertices = sorted(
                {vertex for member in members for vertex in vertex_indices[member]}
            )
            points = [vertices[index] for index in unique_vertices]
            normal = canonical_normal(sum_normal)
            center = tuple(
                sum(point[axis] for point in points) / len(points) for axis in range(3)
            )
            satellite_candidates.append(
                {
                    "patch_index": patch_index,
                    "points": points,
                    "normal": normal,
                    "plane": sum(normal[axis] * center[axis] for axis in range(3)),
                }
            )

        for descriptor in satellite_candidates:
            for host in satellite_hosts:
                if descriptor["patch_index"] == host["patch_index"]:
                    continue
                dot = sum(
                    descriptor["normal"][axis] * host["normal"][axis]
                    for axis in range(3)
                )
                if dot < cosine_limit:
                    continue
                host_plane_values = project(host["points"], descriptor["normal"])
                host_plane = sum(host_plane_values) / len(host_plane_values)
                if abs(descriptor["plane"] - host_plane) > coplanar_lane_maximum_plane_gap_mm:
                    continue
                first, second = plane_basis(descriptor["normal"])
                descriptor_first = project(descriptor["points"], first)
                descriptor_second = project(descriptor["points"], second)
                host_first = project(host["points"], first)
                host_second = project(host["points"], second)
                first_gap = interval_gap(
                    min(descriptor_first),
                    max(descriptor_first),
                    min(host_first),
                    max(host_first),
                )
                second_gap = interval_gap(
                    min(descriptor_second),
                    max(descriptor_second),
                    min(host_second),
                    max(host_second),
                )
                if math.hypot(first_gap, second_gap) <= coplanar_lane_maximum_side_gap_mm:
                    lane_patch_indices.add(descriptor["patch_index"])
                    break

        if layered_feature_maximum_depth_mm is not None:
            layered_candidates = [
                descriptor
                for descriptor in satellite_candidates
                if patches[descriptor["patch_index"]][1]
                >= coplanar_lane_minimum_patch_area_mm2
            ]
            layered_pairs = []
            for left_index, left in enumerate(layered_candidates):
                for right in layered_candidates[left_index + 1 :]:
                    dot = sum(
                        left["normal"][axis] * right["normal"][axis]
                        for axis in range(3)
                    )
                    if dot < cosine_limit:
                        continue
                    right_plane_values = project(right["points"], left["normal"])
                    right_plane = sum(right_plane_values) / len(right_plane_values)
                    plane_gap = abs(left["plane"] - right_plane)
                    if not (
                        coplanar_lane_maximum_plane_gap_mm
                        < plane_gap
                        <= layered_feature_maximum_depth_mm
                    ):
                        continue
                    first, second = plane_basis(left["normal"])
                    left_first = project(left["points"], first)
                    left_second = project(left["points"], second)
                    right_first = project(right["points"], first)
                    right_second = project(right["points"], second)
                    first_overlap = min(max(left_first), max(right_first)) - max(
                        min(left_first), min(right_first)
                    )
                    second_overlap = min(max(left_second), max(right_second)) - max(
                        min(left_second), min(right_second)
                    )
                    if first_overlap > 0.0 and second_overlap > 0.0:
                        layered_patch_indices.add(left["patch_index"])
                        layered_patch_indices.add(right["patch_index"])
                        layered_pairs.append(
                            {
                                "normal": left["normal"],
                                "first": first,
                                "second": second,
                                "plane_min": min(left["plane"], right_plane),
                                "plane_max": max(left["plane"], right_plane),
                                "first_min": max(min(left_first), min(right_first)),
                                "first_max": min(max(left_first), max(right_first)),
                                "second_min": max(min(left_second), min(right_second)),
                                "second_max": min(max(left_second), max(right_second)),
                            }
                        )

            # A repaired shallow feature often has many sub-5mm² bevel and
            # transition fragments between its two confirmed parallel faces.
            # Admit only fragments whose centroid stays inside that face pair's
            # shallow depth slab and overlapping footprint (plus the same small
            # in-plane tolerance used by the lane classifier). This is a bounded
            # spatial closure, not a recursive topology walk.
            for patch_index, (members, total_area, _sum_normal) in enumerate(patches):
                if total_area >= coplanar_lane_minimum_patch_area_mm2:
                    continue
                if not any(selected[member] for member in members):
                    continue
                unique_vertices = sorted(
                    {vertex for member in members for vertex in vertex_indices[member]}
                )
                points = [vertices[index] for index in unique_vertices]
                center = tuple(
                    sum(point[axis] for point in points) / len(points)
                    for axis in range(3)
                )
                for pair in layered_pairs:
                    plane = sum(center[axis] * pair["normal"][axis] for axis in range(3))
                    if not (
                        pair["plane_min"] - coplanar_lane_maximum_plane_gap_mm
                        <= plane
                        <= pair["plane_max"] + coplanar_lane_maximum_plane_gap_mm
                    ):
                        continue
                    first_value = sum(
                        center[axis] * pair["first"][axis] for axis in range(3)
                    )
                    second_value = sum(
                        center[axis] * pair["second"][axis] for axis in range(3)
                    )
                    margin = coplanar_lane_maximum_side_gap_mm
                    if not (
                        pair["first_min"] - margin
                        <= first_value
                        <= pair["first_max"] + margin
                        and pair["second_min"] - margin
                        <= second_value
                        <= pair["second_max"] + margin
                    ):
                        continue
                    layered_patch_indices.add(patch_index)
                    break

    protected = set()
    protected_patch_count = 0
    protected_area = 0.0
    for patch_index, (members, total_area, sum_normal) in enumerate(patches):
        if total_area < minimum_area_mm2 and not compact_vertical_strip(
            members, total_area, sum_normal
        ) and patch_index not in lane_patch_indices and patch_index not in layered_patch_indices:
            continue
        selected_members = [member for member in members if selected[member]]
        if not selected_members:
            continue
        protected_patch_count += 1
        protected_area += sum(areas[member] for member in selected_members)
        for member in selected_members:
            protected.add(source_indices[member])

    if protect_connected_coplanar:
        # A deliberately simple interpretation of a slicer's visible "face":
        # at least two edge-connected triangles occupying the same plane. This
        # is independent of area, aspect ratio, orientation and object-specific
        # coordinates. Compare every candidate to the component seed rather
        # than walking normal-to-normal, preventing gradual curvature from
        # accumulating into a supposedly flat component.
        strict_cosine = math.cos(math.radians(connected_coplanar_angle_degrees))
        strict_visited = [False] * len(areas)
        strict_components = []
        for start in range(len(areas)):
            if strict_visited[start]:
                continue
            strict_visited[start] = True
            members = [start]
            queue = deque([start])
            seed_normal = normals[start]
            seed_point = vertices[vertex_indices[start][0]]
            while queue:
                current = queue.popleft()
                for neighbor in adjacency[current]:
                    if strict_visited[neighbor]:
                        continue
                    dot = sum(
                        seed_normal[axis] * normals[neighbor][axis]
                        for axis in range(3)
                    )
                    if abs(dot) < strict_cosine:
                        continue
                    if any(
                        abs(
                            sum(
                                (vertices[vertex_index][axis] - seed_point[axis])
                                * seed_normal[axis]
                                for axis in range(3)
                            )
                        )
                        > connected_coplanar_plane_tolerance_mm
                        for vertex_index in vertex_indices[neighbor]
                    ):
                        continue
                    strict_visited[neighbor] = True
                    members.append(neighbor)
                    queue.append(neighbor)
            component_points = [
                vertices[vertex_index]
                for vertex_index in {
                    vertex_index
                    for member in members
                    for vertex_index in vertex_indices[member]
                }
            ]
            spans = (0.0, 0.0)
            if len(component_points) >= 2:
                first, second = plane_basis(seed_normal)
                coordinates = list(
                    zip(project(component_points, first), project(component_points, second))
                )
                mean_first = sum(value[0] for value in coordinates) / len(coordinates)
                mean_second = sum(value[1] for value in coordinates) / len(coordinates)
                centered = [
                    (value[0] - mean_first, value[1] - mean_second)
                    for value in coordinates
                ]
                covariance_first = sum(value[0] * value[0] for value in centered)
                covariance_second = sum(value[1] * value[1] for value in centered)
                covariance_cross = sum(value[0] * value[1] for value in centered)
                angle = 0.5 * math.atan2(
                    2.0 * covariance_cross, covariance_first - covariance_second
                )
                cosine = math.cos(angle)
                sine = math.sin(angle)
                aligned_first = [
                    value[0] * cosine + value[1] * sine for value in centered
                ]
                aligned_second = [
                    -value[0] * sine + value[1] * cosine for value in centered
                ]
                spans = sorted(
                    (
                        max(aligned_first) - min(aligned_first),
                        max(aligned_second) - min(aligned_second),
                    )
                )
            bounds = tuple(
                (min(point[axis] for point in component_points),
                 max(point[axis] for point in component_points))
                for axis in range(3)
            )
            qualifies = (
                len(members) >= 2
                and spans[0] >= connected_coplanar_minimum_short_span_mm
                and spans[1] >= connected_coplanar_minimum_long_span_mm
            )
            strict_components.append(
                (members, component_points, seed_normal, seed_point, bounds, qualifies)
            )

        qualifying_components = [
            component for component in strict_components if component[5]
        ]
        satellite_cell_size = max(1.0, connected_coplanar_satellite_gap_mm * 4.0)

        def satellite_cells(component_bounds, margin=0.0):
            ranges = [
                range(
                    math.floor((component_bounds[axis][0] - margin) / satellite_cell_size),
                    math.floor((component_bounds[axis][1] + margin) / satellite_cell_size) + 1,
                )
                for axis in range(3)
            ]
            for x in ranges[0]:
                for y in ranges[1]:
                    for z in ranges[2]:
                        yield (x, y, z)

        anchor_grid = {}
        for anchor_index, anchor in enumerate(qualifying_components):
            for cell in satellite_cells(anchor[4], connected_coplanar_satellite_gap_mm):
                anchor_grid.setdefault(cell, []).append(anchor_index)

        for component in strict_components:
            members, component_points, seed_normal, seed_point, bounds, qualifies = component
            if not qualifies and connected_coplanar_satellite_gap_mm > 0.0:
                nearby_anchor_indices = {
                    anchor_index
                    for cell in satellite_cells(bounds)
                    for anchor_index in anchor_grid.get(cell, ())
                }
                for anchor_index in nearby_anchor_indices:
                    anchor = qualifying_components[anchor_index]
                    _, anchor_points, anchor_normal, anchor_point, anchor_bounds, _ = anchor
                    axis_gaps = [
                        max(0.0, anchor_bounds[axis][0] - bounds[axis][1],
                            bounds[axis][0] - anchor_bounds[axis][1])
                        for axis in range(3)
                    ]
                    if math.sqrt(sum(gap * gap for gap in axis_gaps)) > connected_coplanar_satellite_gap_mm:
                        continue
                    dot = abs(sum(seed_normal[axis] * anchor_normal[axis] for axis in range(3)))
                    if dot < strict_cosine:
                        continue
                    if any(
                        abs(sum((point[axis] - anchor_point[axis]) * anchor_normal[axis] for axis in range(3)))
                        > connected_coplanar_plane_tolerance_mm
                        for point in component_points
                    ):
                        continue
                    qualifies = True
                    break
            if not qualifies:
                continue
            newly_protected = [
                member
                for member in members
                if selected[member] and source_indices[member] not in protected
            ]
            if not newly_protected:
                continue
            protected_patch_count += 1
            protected_area += sum(areas[member] for member in newly_protected)
            protected.update(source_indices[member] for member in newly_protected)
    return protected, protected_patch_count, protected_area


def _point_to_triangle_distance_approx(
    point: Tuple[float, float, float], triangle: Tuple[Tuple[float, float, float], ...]
) -> float:
    """Cheap lower-effort stand-in for exact point-to-triangle distance.

    Checks the 3 vertices and the 3 edge midpoints only, not the true
    nearest point on the triangle's surface. That slightly overestimates
    distance in some orientations, but it is a reasonable approximation at
    the sub-millimetre "are these basically touching" scale this function
    cares about, and is far cheaper than an exact point-in-triangle
    projection when run over hundreds of thousands of candidate pairs.
    """
    candidates = list(triangle)
    for i in range(3):
        a = triangle[i]
        b = triangle[(i + 1) % 3]
        candidates.append(tuple((a[axis] + b[axis]) / 2 for axis in range(3)))
    return min(math.dist(point, candidate) for candidate in candidates)


def _find_overlap_protection(
    object_xml: bytes,
    *,
    max_gap_mm: float = 0.3,
    depth_mm: float = 0.3,
    cell_size_mm: float = 1.0,
) -> Tuple[Set[int], dict[int, float]]:
    """Find triangles belonging to a geometrically disconnected piece of the
    same object that physically touches/crosses another piece, regardless
    of paint colour, and never fuzz them.

    Protection is found in two passes, both using the same tight
    `max_gap_mm` by default (`depth_mm` defaults to equal `max_gap_mm`,
    making pass two a no-op re-check of pass one). The two-pass structure
    exists for `depth_mm` to be raised above `max_gap_mm` later if a real
    case needs it, but raising it was tried on a real model and rejected:
    kitbashed rock chunks sit close to their *own* neighbours across most
    of their own surface (that is how a packed rock wall is built), so
    once two shells are confirmed touching anywhere, widening the search
    radius to "anything else nearby" does not find just the embedded tip
    -- on a real 30,748-triangle test piece, `depth_mm=1.0` already
    protected 34.6% of the whole mesh, `depth_mm=3.0` protected 76.6%,
    climbing almost linearly with no natural plateau. There is no fixed
    Euclidean radius that separates "the part of this rock actually buried
    in the wall" from "the rest of this small rock that merely sits near
    its neighbours," because both are the same few millimetres across. A
    correct version of "protect deeper" would have to grow along the
    confirmed-touching shell's own connected surface outward from the
    actual contact point (a geodesic flood-fill), not by straight-line
    distance to the other shell -- not implemented here.

    Hand-sculpted/kitbashed terrain models are frequently exported as many
    separate stone/rock chunks merged into one object without ever being
    welded (boolean-unioned) into a single watertight shell. On a real
    model from this category, the user found -- by hand, dragging pieces
    apart in Blender after splitting the mesh by loose parts -- three small
    rock chunks that were never meant to touch a nearby wall at all, but
    were positioned slightly overlapping/crossing into it. Paint applied to
    the wall underneath does nothing to fix this, because the slicer may
    pick up either surface, and the original designer has no practical way
    to notice or paint around debris placement like this.

    Two cheaper signals were tried and both failed real-model verification
    before this one. Plain proximity ("is there some other triangle
    nearby") is not usable: at every ordinary paint-colour boundary, an
    unpainted triangle sits immediately next to a painted one, and that is
    completely normal -- on a real model this matched over 98% of all
    triangles. Requiring the two triangles to additionally be near-parallel
    and have overlapping 2D footprints (a flush duplicate layer) is more
    selective, but still misses genuinely separate colliding chunks that
    cross through a surface at an angle rather than lying flat on it, which
    is exactly the shape of the real defect found by hand.

    The distinguishing feature that actually holds up is geometric
    disconnection: triangles are grouped into "shells" with a union-find
    over shared vertex *positions* (so a welded seam that is still really
    one piece is not split apart -- this matches what Bambu Studio's own
    "split into parts" and Blender's "Separate by Loose Parts" both treat
    as one piece). Only candidate pairs from two different shells are
    tested, and the test is simply: is any part of one triangle within
    `max_gap_mm` of the other (checked against its 3 vertices and 3 edge
    midpoints, cheaply)? No angle or orientation requirement -- a stray
    chunk colliding with a wall at any angle still should not be fuzzed
    where it collides. On the real model this isolated a similar ~4% of
    all triangles to the flush-only version, not a wholesale re-flagging
    of ordinary geometry, and real debris chunks the user confirmed by hand
    fall within this category.

    This does not catch every kind of mesh debris -- a small patch that is
    technically welded to the real surface at a few shared vertices (seen
    on the same real model, in a different corner) is, by this definition,
    the same connected piece, and is left alone. That is a real, different,
    rarer problem than the one this function targets.
    """
    vertices = []
    for match in VERTEX_RE.finditer(object_xml):
        tag = match.group(0)
        coords = [_simple_attribute(tag, axis) for axis in (b"x", b"y", b"z")]
        if any(value is None for value in coords):
            return set(), {}
        vertices.append(tuple(float(value) for value in coords))
    if not vertices:
        return set(), {}

    def position_key(index: int) -> Tuple[float, float, float]:
        return tuple(round(value, 4) for value in vertices[index])

    source_indices = []
    areas = []
    centroids = []
    triangle_points = []
    position_sets: List[frozenset] = []

    for triangle_index, match in enumerate(TRIANGLE_RE.finditer(object_xml)):
        tag = match.group(0)
        raw_indices = [_simple_attribute(tag, key) for key in (b"v1", b"v2", b"v3")]
        if any(value is None for value in raw_indices):
            continue
        indices = tuple(int(value) for value in raw_indices)
        if any(index < 0 or index >= len(vertices) for index in indices):
            continue
        a, b, c = (vertices[index] for index in indices)
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in cross))
        if length <= 1e-12:
            continue
        source_indices.append(triangle_index)
        areas.append(length * 0.5)
        centroids.append(tuple((a[i] + b[i] + c[i]) / 3 for i in range(3)))
        triangle_points.append((a, b, c))
        position_sets.append(
            frozenset((position_key(indices[0]), position_key(indices[1]), position_key(indices[2])))
        )

    count = len(source_indices)
    if count == 0:
        return set(), {}

    # Union-find over shared vertex positions: two triangles are the same
    # "shell" if reachable from one another via a chain of shared mesh
    # edges/vertices, regardless of angle or paint.
    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[root_first] = root_second

    position_owner: dict = {}
    for candidate, positions in enumerate(position_sets):
        for position in positions:
            owner = position_owner.get(position)
            if owner is None:
                position_owner[position] = candidate
            else:
                union(candidate, owner)

    shell_of = [find(index) for index in range(count)]

    # Index each triangle into every grid cell touched by any of its 3
    # vertices (not just one cell derived from its centroid). A long, thin
    # triangle -- one vertex touching a surface, the other two far away, the
    # real shape of a stray chunk's corner poking into a wall -- has a
    # centroid nowhere near the actual touching point; indexing by centroid
    # alone can miss exactly the collisions this function exists to find.
    def cell_index(point: Tuple[float, float, float]) -> Tuple[int, int, int]:
        return (
            math.floor(point[0] / cell_size_mm),
            math.floor(point[1] / cell_size_mm),
            math.floor(point[2] / cell_size_mm),
        )

    grid: dict = {}
    triangle_cells: List[Set[Tuple[int, int, int]]] = []
    for candidate, points in enumerate(triangle_points):
        cells = {cell_index(point) for point in points}
        triangle_cells.append(cells)
        for cell in cells:
            grid.setdefault(cell, []).append(candidate)

    def neighbours(cells: Set[Tuple[int, int, int]]) -> Iterable[int]:
        seen: Set[int] = set()
        for cx, cy, cz in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for other in grid.get((cx + dx, cy + dy, cz + dz), ()):
                            if other not in seen:
                                seen.add(other)
                                yield other

    # Pass one: which pairs of shells actually touch, at the tight gap.
    touching_shell_pairs: Set[Tuple[int, int]] = set()
    for candidate in range(count):
        shell = shell_of[candidate]
        centroid = centroids[candidate]
        for other in neighbours(triangle_cells[candidate]):
            if other == candidate or shell_of[other] == shell:
                continue
            if _point_to_triangle_distance_approx(centroid, triangle_points[other]) > max_gap_mm:
                continue
            touching_shell_pairs.add(tuple(sorted((shell, shell_of[other]))))

    if not touching_shell_pairs:
        return set(), {}

    touching_shells = {shell for pair in touching_shell_pairs for shell in pair}

    # Pass two: for triangles belonging to a confirmed-touching shell, widen
    # the search to depth_mm, but only count a hit against the specific
    # other shell that pair was confirmed touching in pass one.
    cell_radius = max(1, math.ceil(depth_mm / cell_size_mm))

    def neighbours_wide(cells: Set[Tuple[int, int, int]]) -> Iterable[int]:
        seen: Set[int] = set()
        for cx, cy, cz in cells:
            for dx in range(-cell_radius, cell_radius + 1):
                for dy in range(-cell_radius, cell_radius + 1):
                    for dz in range(-cell_radius, cell_radius + 1):
                        for other in grid.get((cx + dx, cy + dy, cz + dz), ()):
                            if other not in seen:
                                seen.add(other)
                                yield other

    flagged = [False] * count
    for candidate in range(count):
        shell = shell_of[candidate]
        if shell not in touching_shells:
            continue
        centroid = centroids[candidate]
        for other in neighbours_wide(triangle_cells[candidate]):
            if other == candidate:
                continue
            other_shell = shell_of[other]
            if other_shell == shell:
                continue
            if tuple(sorted((shell, other_shell))) not in touching_shell_pairs:
                continue
            if _point_to_triangle_distance_approx(centroid, triangle_points[other]) > depth_mm:
                continue
            flagged[candidate] = True
            break

    protected = {source_indices[index] for index in range(count) if flagged[index]}
    area_by_index = {source_indices[index]: areas[index] for index in range(count) if flagged[index]}
    return protected, area_by_index


def _find_flat_contact_protection(
    object_xml: bytes,
    slots: Optional[Set[int]],
    *,
    max_gap_mm: float = 0.05,
    angle_degrees: float = 2.0,
    minimum_overlap_area_mm2: float = 0.01,
    cell_size_mm: float = 1.0,
    geometry: Optional[_MeshGeometry] = None,
) -> Tuple[Set[int], dict[int, float]]:
    """Protect painted triangles whose faces truly lie flat against another face.

    This deliberately tests the contact footprint itself rather than treating
    disconnected shells, proximity, or non-manifold topology as intent. Faces
    may belong to the same messy shell or to separate objects. A pair qualifies
    only when its normals are nearly parallel/anti-parallel, its plane gap is
    near zero, and its projected triangles overlap by positive area. Only the
    participating painted triangles are protected; protection never grows to
    their surrounding flat patches.
    """
    geometry = geometry or _parse_mesh_geometry(object_xml, slots)
    if geometry is None:
        return set(), {}
    source_indices = geometry.source_indices
    areas = geometry.areas
    normals = geometry.normals
    triangle_points = geometry.triangle_points
    selected = geometry.selected
    bounds = geometry.bounds

    count = len(source_indices)
    if count == 0 or not any(selected):
        return set(), {}

    def cells_for(candidate: int) -> Iterable[Tuple[int, int, int]]:
        minimum, maximum = bounds[candidate]
        ranges = [
            range(
                math.floor((minimum[axis] - max_gap_mm) / cell_size_mm),
                math.floor((maximum[axis] + max_gap_mm) / cell_size_mm) + 1,
            )
            for axis in range(3)
        ]
        for x in ranges[0]:
            for y in ranges[1]:
                for z in ranges[2]:
                    yield (x, y, z)

    grid: dict = {}
    triangle_cells = []
    for candidate in range(count):
        cells = tuple(cells_for(candidate))
        triangle_cells.append(cells)
        for cell in cells:
            grid.setdefault(cell, []).append(candidate)

    def polygon_area(points: List[Tuple[float, float]]) -> float:
        return 0.5 * abs(
            sum(
                points[index][0] * points[(index + 1) % len(points)][1]
                - points[(index + 1) % len(points)][0] * points[index][1]
                for index in range(len(points))
            )
        ) if len(points) >= 3 else 0.0

    def clipped_overlap_area(
        subject: List[Tuple[float, float]], clip: List[Tuple[float, float]]
    ) -> float:
        orientation = sum(
            clip[index][0] * clip[(index + 1) % 3][1]
            - clip[(index + 1) % 3][0] * clip[index][1]
            for index in range(3)
        )
        if orientation < 0.0:
            clip = list(reversed(clip))
        output = subject
        for edge_index in range(3):
            edge_start = clip[edge_index]
            edge_end = clip[(edge_index + 1) % 3]
            input_points = output
            output = []
            if not input_points:
                break

            def side(point: Tuple[float, float]) -> float:
                return (
                    (edge_end[0] - edge_start[0]) * (point[1] - edge_start[1])
                    - (edge_end[1] - edge_start[1]) * (point[0] - edge_start[0])
                )

            previous = input_points[-1]
            previous_side = side(previous)
            for current in input_points:
                current_side = side(current)
                if current_side >= -1e-9:
                    if previous_side < -1e-9:
                        denominator = previous_side - current_side
                        ratio = previous_side / denominator
                        output.append(
                            (
                                previous[0] + ratio * (current[0] - previous[0]),
                                previous[1] + ratio * (current[1] - previous[1]),
                            )
                        )
                    output.append(current)
                elif previous_side >= -1e-9:
                    denominator = previous_side - current_side
                    ratio = previous_side / denominator
                    output.append(
                        (
                            previous[0] + ratio * (current[0] - previous[0]),
                            previous[1] + ratio * (current[1] - previous[1]),
                        )
                    )
                previous = current
                previous_side = current_side
        return polygon_area(output)

    cosine_limit = math.cos(math.radians(angle_degrees))
    flagged: Set[int] = set()
    checked_pairs: Set[Tuple[int, int]] = set()
    for candidate in range(count):
        if not selected[candidate]:
            continue
        candidate_minimum, candidate_maximum = bounds[candidate]
        nearby: Set[int] = set()
        for cell in triangle_cells[candidate]:
            nearby.update(grid.get(cell, ()))
        for other in nearby:
            if other == candidate:
                continue
            other_minimum, other_maximum = bounds[other]
            if any(
                candidate_maximum[axis] + max_gap_mm < other_minimum[axis]
                or other_maximum[axis] + max_gap_mm < candidate_minimum[axis]
                for axis in range(3)
            ):
                continue
            pair = tuple(sorted((candidate, other)))
            if pair in checked_pairs:
                continue
            checked_pairs.add(pair)
            dot = sum(normals[candidate][axis] * normals[other][axis] for axis in range(3))
            if abs(dot) < cosine_limit:
                continue
            relative = tuple(
                triangle_points[candidate][0][axis] - triangle_points[other][0][axis]
                for axis in range(3)
            )
            gap = abs(sum(relative[axis] * normals[other][axis] for axis in range(3)))
            if gap > max_gap_mm:
                continue
            drop_axis = max(range(3), key=lambda axis: abs(normals[candidate][axis]))
            keep_axes = [axis for axis in range(3) if axis != drop_axis]
            first = [
                (point[keep_axes[0]], point[keep_axes[1]])
                for point in triangle_points[candidate]
            ]
            second = [
                (point[keep_axes[0]], point[keep_axes[1]])
                for point in triangle_points[other]
            ]
            projected_overlap = clipped_overlap_area(first, second)
            normal_scale = abs(normals[candidate][drop_axis])
            if normal_scale <= 1e-9:
                continue
            overlap_area = projected_overlap / normal_scale
            if overlap_area < minimum_overlap_area_mm2:
                continue
            flagged.add(candidate)
            if selected[other]:
                flagged.add(other)

    protected = {source_indices[index] for index in flagged}
    area_by_index = {source_indices[index]: areas[index] for index in flagged}
    return protected, area_by_index


def _find_fuzz_envelope_protection(
    object_xml: bytes,
    slots: Optional[Set[int]],
    *,
    distance_mm: float,
    contact_seed_indices: Optional[Set[int]] = None,
    contact_neighbor_rings: int = 1,
    cell_size_mm: float = 2.0,
    geometry: Optional[_MeshGeometry] = None,
) -> Tuple[Set[int], dict[int, float]]:
    """Protect painted faces whose fuzzy displacement can hit another face.

    A coincident contact test catches the face lying against an obstruction,
    but not the angled faces surrounding it.  Their displaced fuzzy points can
    still poke through the obstruction.  Sample each painted triangle's
    interior and cast a segment in both normal directions for the configured
    fuzzy thickness.  A real segment/triangle hit protects only that source
    triangle.  Hits at the segment origin are ignored, so ordinary shared
    edges and vertices do not qualify by themselves.
    """
    if distance_mm <= 0.0:
        return set(), {}

    geometry = geometry or _parse_mesh_geometry(object_xml, slots)
    if geometry is None:
        return set(), {}
    vertices = geometry.vertices
    source_indices = geometry.source_indices
    triangle_points = geometry.triangle_points
    triangle_vertex_indices = geometry.vertex_indices
    normals = geometry.normals
    areas = geometry.areas
    selected = geometry.selected
    bounds = geometry.bounds

    if not any(selected):
        return set(), {}

    eligible: Optional[Set[int]] = None
    if contact_seed_indices is not None:
        source_to_internal = {
            source_index: internal_index
            for internal_index, source_index in enumerate(source_indices)
        }
        seed_internal = {
            source_to_internal[source_index]
            for source_index in contact_seed_indices
            if source_index in source_to_internal
        }
        if not seed_internal:
            return set(), {}
        edge_owners: dict = {}
        adjacency: List[Set[int]] = [set() for _ in source_indices]

        def point_key(vertex_index: int):
            return tuple(round(value, 4) for value in vertices[vertex_index])

        for internal_index, indices in enumerate(triangle_vertex_indices):
            for left, right in ((0, 1), (1, 2), (2, 0)):
                edge = tuple(sorted((point_key(indices[left]), point_key(indices[right]))))
                for owner in edge_owners.get(edge, ()):
                    adjacency[internal_index].add(owner)
                    adjacency[owner].add(internal_index)
                edge_owners.setdefault(edge, []).append(internal_index)
        eligible = set(seed_internal)
        frontier = set(seed_internal)
        for _ in range(max(0, contact_neighbor_rings)):
            frontier = {
                neighbor
                for current in frontier
                for neighbor in adjacency[current]
                if neighbor not in eligible
            }
            if not frontier:
                break
            eligible.update(frontier)

    def cells_for(minimum, maximum, margin=0.0):
        ranges = [
            range(
                math.floor((minimum[axis] - margin) / cell_size_mm),
                math.floor((maximum[axis] + margin) / cell_size_mm) + 1,
            )
            for axis in range(3)
        ]
        for x in ranges[0]:
            for y in ranges[1]:
                for z in ranges[2]:
                    yield (x, y, z)

    grid: dict[Tuple[int, int, int], List[int]] = {}
    for triangle_index, (minimum, maximum) in enumerate(bounds):
        for cell in cells_for(minimum, maximum):
            grid.setdefault(cell, []).append(triangle_index)

    def cross_product(left, right):
        return (
            left[1] * right[2] - left[2] * right[1],
            left[2] * right[0] - left[0] * right[2],
            left[0] * right[1] - left[1] * right[0],
        )

    def segment_hits_triangle(origin, direction, target) -> bool:
        first, second, third = target
        edge_one = tuple(second[axis] - first[axis] for axis in range(3))
        edge_two = tuple(third[axis] - first[axis] for axis in range(3))
        h = cross_product(direction, edge_two)
        determinant = sum(edge_one[axis] * h[axis] for axis in range(3))
        if abs(determinant) <= 1e-10:
            return False
        inverse = 1.0 / determinant
        offset = tuple(origin[axis] - first[axis] for axis in range(3))
        u = inverse * sum(offset[axis] * h[axis] for axis in range(3))
        if u < -1e-9 or u > 1.0 + 1e-9:
            return False
        q = cross_product(offset, edge_one)
        v = inverse * sum(direction[axis] * q[axis] for axis in range(3))
        if v < -1e-9 or u + v > 1.0 + 1e-9:
            return False
        travel = inverse * sum(edge_two[axis] * q[axis] for axis in range(3))
        return 1e-5 < travel <= distance_mm + 1e-9

    sample_weights = (
        (1 / 3, 1 / 3, 1 / 3),
        (0.80, 0.10, 0.10),
        (0.10, 0.80, 0.10),
        (0.10, 0.10, 0.80),
        (0.45, 0.45, 0.10),
        (0.10, 0.45, 0.45),
        (0.45, 0.10, 0.45),
    )
    flagged: Set[int] = set()
    for candidate, is_selected in enumerate(selected):
        if not is_selected:
            continue
        if eligible is not None and candidate not in eligible:
            continue
        minimum, maximum = bounds[candidate]
        nearby: Set[int] = set()
        for cell in cells_for(minimum, maximum, distance_mm):
            nearby.update(grid.get(cell, ()))
        nearby.discard(candidate)
        if not nearby:
            continue
        points = triangle_points[candidate]
        normal = normals[candidate]
        swept_minimum = tuple(
            minimum[axis] - abs(normal[axis]) * distance_mm for axis in range(3)
        )
        swept_maximum = tuple(
            maximum[axis] + abs(normal[axis]) * distance_mm for axis in range(3)
        )
        samples = [
            tuple(
                sum(weights[index] * points[index][axis] for index in range(3))
                for axis in range(3)
            )
            for weights in sample_weights
        ]
        directions = (normal, tuple(-value for value in normal))
        hit = False
        for other in nearby:
            if set(triangle_vertex_indices[candidate]) & set(triangle_vertex_indices[other]):
                continue
            other_minimum, other_maximum = bounds[other]
            if any(
                other_maximum[axis] < swept_minimum[axis] - 1e-9
                or other_minimum[axis] > swept_maximum[axis] + 1e-9
                for axis in range(3)
            ):
                continue
            target = triangle_points[other]
            for sample in samples:
                if any(segment_hits_triangle(sample, direction, target) for direction in directions):
                    hit = True
                    break
            if hit:
                break
        if hit:
            flagged.add(candidate)

    protected = {source_indices[index] for index in flagged}
    area_by_index = {source_indices[index]: areas[index] for index in flagged}
    return protected, area_by_index


def _find_coincident_protection(
    object_xml: bytes,
    *,
    max_gap_mm: float = 0.05,
    angle_degrees: float = 2.0,
    cell_size_mm: float = 1.0,
    evidence_radius_mm: float = 2.0,
) -> Tuple[Set[int], dict[int, float]]:
    """Find flat surfaces of two disconnected pieces sitting right on top of
    each other -- true coincident/z-fighting geometry, not merely close --
    and never fuzz either one, for the whole flat surface each belongs to.

    A third real defect category, distinct from both `_find_overlap_protection`
    (disconnected pieces crossing at an angle) and `_find_nonmanifold_protection`
    (a patch welded into the same shell). Here the two pieces are still
    genuinely different shells, but instead of poking through each other at
    an angle, one piece's flat face sits almost exactly on top of the
    other's, which is what actually produces z-fighting/flicker in a 3D
    viewer: the renderer cannot decide which coincident face is in front.
    The real example that prompted this was a mortise joint's own flat
    walls, a functional surface, not rough decoration -- deliberately
    never meant to carry fuzzy texture at all.

    Detection requires three things to agree, keeping this far more
    selective than plain proximity: the two triangles are in different
    shells (disconnected pieces, the same union-find used elsewhere in this
    module), their normals are parallel or anti-parallel within
    `angle_degrees` (genuinely coplanar, not just nearby), and the
    perpendicular gap between them is tiny (`max_gap_mm`, far tighter than
    `_find_overlap_protection`'s default -- this is near-zero coincidence,
    not a loose touch).

    Once a seed pair is found, protection is NOT limited to the seed
    triangles themselves: each side's flat patch is grown outward from the
    seed using the same area-weighted-average-normal region growth as
    `_find_planar_protection`, so the whole coincident surface is protected,
    not just the handful of triangles that happened to be close enough to
    trigger detection. This naturally stays bounded where a straight-line
    search radius (tried and rejected -- see `_find_overlap_protection`'s
    docstring) was not: a flat patch stops growing at a real edge or corner,
    so it cannot swallow an unrelated, differently-angled part of the same
    piece the way widening a Euclidean radius did. Validated against a real
    exported model section (wall + 7 loose stones): 6.2% of triangles
    flagged across 137 separate grown patches, the largest only 275mm²,
    not one patch swallowing the whole piece.

    Geometry alone cannot tell "defect" apart from "fine": on the real
    production model this also fired on several legitimately loose
    decorative pieces (ledges/battlements) that were simply never welded
    into the core and happen to sit flush against it -- by flatness and
    coincidence alone, that looks identical to the mortise-wall defect.
    The distinguishing evidence found by hand was that the model's loose
    *decoration* is otherwise clean, manifold geometry, while the actual
    defect pieces (the ones the user introduced while editing, e.g. the
    "sliver-fan cluster") carry scarred, non-manifold geometry of their
    own -- see `_find_nonmanifold_protection`. A seed pair is only kept
    when a non-manifold edge exists within `evidence_radius_mm` of either
    triangle. This is deliberately local, not "does either shell contain
    a non-manifold edge anywhere": checking the whole shell would make
    the huge core shell count as "has evidence" the moment it has even
    one unrelated scar anywhere else on the model, which defeats the
    point for every coincident pair that touches the core (nearly all of
    them). A clean loose decoration's coincident face has no non-manifold
    edge anywhere nearby and is left alone; actual debris does.
    """
    vertices = []
    for match in VERTEX_RE.finditer(object_xml):
        tag = match.group(0)
        coords = [_simple_attribute(tag, axis) for axis in (b"x", b"y", b"z")]
        if any(value is None for value in coords):
            return set(), {}
        vertices.append(tuple(float(value) for value in coords))
    if not vertices:
        return set(), {}

    def position_key(index: int) -> Tuple[float, float, float]:
        return tuple(round(value, 4) for value in vertices[index])

    source_indices: List[int] = []
    areas: List[float] = []
    normals: List[Tuple[float, float, float]] = []
    centroids: List[Tuple[float, float, float]] = []
    triangle_points: List[Tuple[Tuple[float, float, float], ...]] = []
    position_sets: List[frozenset] = []
    adjacency: List[List[int]] = []
    edge_owner: dict = {}

    for triangle_index, match in enumerate(TRIANGLE_RE.finditer(object_xml)):
        tag = match.group(0)
        raw_indices = [_simple_attribute(tag, key) for key in (b"v1", b"v2", b"v3")]
        if any(value is None for value in raw_indices):
            continue
        indices = tuple(int(value) for value in raw_indices)
        if any(index < 0 or index >= len(vertices) for index in indices):
            continue
        a, b, c = (vertices[index] for index in indices)
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in cross))
        if length <= 1e-12:
            continue
        candidate = len(areas)
        source_indices.append(triangle_index)
        areas.append(length * 0.5)
        normals.append(tuple(value / length for value in cross))
        centroids.append(tuple((a[i] + b[i] + c[i]) / 3 for i in range(3)))
        triangle_points.append((a, b, c))
        position_sets.append(
            frozenset((position_key(indices[0]), position_key(indices[1]), position_key(indices[2])))
        )
        adjacency.append([])
        for edge in (
            tuple(sorted((position_key(indices[0]), position_key(indices[1])))),
            tuple(sorted((position_key(indices[1]), position_key(indices[2])))),
            tuple(sorted((position_key(indices[2]), position_key(indices[0])))),
        ):
            neighbor = edge_owner.get(edge)
            if neighbor is None:
                edge_owner[edge] = candidate
            else:
                adjacency[neighbor].append(candidate)
                adjacency[candidate].append(neighbor)

    count = len(areas)
    if count == 0:
        return set(), {}

    parent = list(range(count))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(first: int, second: int) -> None:
        root_first, root_second = find(first), find(second)
        if root_first != root_second:
            parent[root_first] = root_second

    position_owner: dict = {}
    for candidate, positions in enumerate(position_sets):
        for position in positions:
            owner = position_owner.get(position)
            if owner is None:
                position_owner[position] = candidate
            else:
                union(candidate, owner)

    shell_of = [find(index) for index in range(count)]

    nonmanifold_indices, _ = _find_nonmanifold_protection(object_xml)

    def cell_index(point: Tuple[float, float, float]) -> Tuple[int, int, int]:
        return (
            math.floor(point[0] / cell_size_mm),
            math.floor(point[1] / cell_size_mm),
            math.floor(point[2] / cell_size_mm),
        )

    grid: dict = {}
    triangle_cells: List[Set[Tuple[int, int, int]]] = []
    for candidate, points in enumerate(triangle_points):
        cells = {cell_index(point) for point in points}
        triangle_cells.append(cells)
        for cell in cells:
            grid.setdefault(cell, []).append(candidate)

    def neighbours(cells: Set[Tuple[int, int, int]]) -> Iterable[int]:
        seen: Set[int] = set()
        for cx, cy, cz in cells:
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for other in grid.get((cx + dx, cy + dy, cz + dz), ()):
                            if other not in seen:
                                seen.add(other)
                                yield other

    evidence_cell_radius = max(1, math.ceil(evidence_radius_mm / cell_size_mm))
    evidence_grid: dict = {}
    for candidate in range(count):
        if source_indices[candidate] not in nonmanifold_indices:
            continue
        for cell in triangle_cells[candidate]:
            evidence_grid.setdefault(cell, []).append(candidate)

    def has_nearby_evidence(cells: Set[Tuple[int, int, int]]) -> bool:
        for cx, cy, cz in cells:
            for dx in range(-evidence_cell_radius, evidence_cell_radius + 1):
                for dy in range(-evidence_cell_radius, evidence_cell_radius + 1):
                    for dz in range(-evidence_cell_radius, evidence_cell_radius + 1):
                        if evidence_grid.get((cx + dx, cy + dy, cz + dz)):
                            return True
        return False

    cosine_limit = math.cos(math.radians(angle_degrees))
    seeds: Set[int] = set()
    for candidate in range(count):
        shell = shell_of[candidate]
        centroid = centroids[candidate]
        for other in neighbours(triangle_cells[candidate]):
            if other == candidate or shell_of[other] == shell:
                continue
            dot = sum(normals[candidate][axis] * normals[other][axis] for axis in range(3))
            if abs(dot) < cosine_limit:
                continue
            reference_point = triangle_points[other][0]
            relative = tuple(centroid[axis] - reference_point[axis] for axis in range(3))
            gap = abs(sum(relative[axis] * normals[other][axis] for axis in range(3)))
            if gap > max_gap_mm:
                continue
            if not has_nearby_evidence(triangle_cells[candidate]) and not has_nearby_evidence(
                triangle_cells[other]
            ):
                continue
            seeds.add(candidate)

    if not seeds:
        return set(), {}

    visited = [False] * count
    protected: Set[int] = set()
    area_by_index: dict = {}
    for seed in seeds:
        if visited[seed]:
            continue
        members = [seed]
        visited[seed] = True
        sum_normal = [normals[seed][axis] * areas[seed] for axis in range(3)]
        queue = deque([seed])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency[current]:
                if visited[neighbor]:
                    continue
                length = math.sqrt(sum(value * value for value in sum_normal))
                if length <= 1e-12:
                    continue
                reference = tuple(value / length for value in sum_normal)
                dot = sum(reference[axis] * normals[neighbor][axis] for axis in range(3))
                if dot < cosine_limit:
                    continue
                visited[neighbor] = True
                members.append(neighbor)
                queue.append(neighbor)
                for axis in range(3):
                    sum_normal[axis] += normals[neighbor][axis] * areas[neighbor]
        for member in members:
            protected.add(source_indices[member])
            area_by_index[source_indices[member]] = areas[member]

    return protected, area_by_index


def _find_nonmanifold_protection(object_xml: bytes) -> Tuple[Set[int], dict[int, float]]:
    """Find triangles touching a non-manifold edge and never fuzz them.

    A different real defect from the one `_find_overlap_protection` targets:
    a small patch of degenerate geometry that is genuinely welded to the
    real surface at a few shared vertices (confirmed on the same real
    model, in a corner where a colliding chunk -- since moved away by the
    user -- left behind a scarred ridge stitched into the wall itself).
    Because it shares real vertices with the wall, it is the same connected
    "shell" as the wall by any shell/disconnection test, so
    `_find_overlap_protection`'s disconnected-piece check correctly leaves
    it alone; a different signal is needed.

    On a proper single surface, every edge is shared by exactly two
    triangles (or one, at an open/boundary edge -- both are completely
    normal and not a defect). An edge shared by three or more triangles
    means something extra was stitched on right there: exactly the
    fingerprint of a scarred-in remnant like the one found by hand, and a
    well-established mesh-validity check in its own right, independent of
    any proximity or angle heuristic. Any triangle touching such an edge is
    protected, on the same reasoning used elsewhere in this file: a human
    looking at a small anomaly stitched onto an otherwise flat, already
    fuzzy-painted area reads the whole thing as "that flat area, with a
    rough spot," not as a separate feature worth keeping fuzzy on its own.

    On the one real model this was checked against, this isolated 5.6% of
    all triangles -- a plausible, bounded amount, not a wholesale
    re-flagging of ordinary geometry. It could not be directly verified
    against the specific hand-found defect cluster from earlier in the same
    session, because round-tripping that cluster through Blender to extract
    it welded together exactly the duplicated vertex positions this check
    depends on, destroying the signal in that one sample -- a limitation of
    the verification method available at the time, not a claim that this
    check is proven against that specific cluster.
    """
    vertices = []
    for match in VERTEX_RE.finditer(object_xml):
        tag = match.group(0)
        coords = [_simple_attribute(tag, axis) for axis in (b"x", b"y", b"z")]
        if any(value is None for value in coords):
            return set(), {}
        vertices.append(tuple(float(value) for value in coords))
    if not vertices:
        return set(), {}

    def position_key(index: int) -> Tuple[float, float, float]:
        return tuple(round(value, 4) for value in vertices[index])

    source_indices = []
    areas = []
    edge_lists: List[Tuple[Tuple, Tuple, Tuple]] = []

    for triangle_index, match in enumerate(TRIANGLE_RE.finditer(object_xml)):
        tag = match.group(0)
        raw_indices = [_simple_attribute(tag, key) for key in (b"v1", b"v2", b"v3")]
        if any(value is None for value in raw_indices):
            continue
        indices = tuple(int(value) for value in raw_indices)
        if any(index < 0 or index >= len(vertices) for index in indices):
            continue
        a, b, c = (vertices[index] for index in indices)
        ab = tuple(b[i] - a[i] for i in range(3))
        ac = tuple(c[i] - a[i] for i in range(3))
        cross = (
            ab[1] * ac[2] - ab[2] * ac[1],
            ab[2] * ac[0] - ab[0] * ac[2],
            ab[0] * ac[1] - ab[1] * ac[0],
        )
        length = math.sqrt(sum(value * value for value in cross))
        if length <= 1e-12:
            continue
        pa, pb, pc = position_key(indices[0]), position_key(indices[1]), position_key(indices[2])
        source_indices.append(triangle_index)
        areas.append(length * 0.5)
        edge_lists.append(
            (tuple(sorted((pa, pb))), tuple(sorted((pb, pc))), tuple(sorted((pc, pa))))
        )

    edge_counts: dict = {}
    for edges in edge_lists:
        for edge in edges:
            edge_counts[edge] = edge_counts.get(edge, 0) + 1

    nonmanifold_edges = {edge for edge, count in edge_counts.items() if count >= 3}
    if not nonmanifold_edges:
        return set(), {}

    protected = set()
    area_by_index: dict = {}
    for index, edges in enumerate(edge_lists):
        if any(edge in nonmanifold_edges for edge in edges):
            protected.add(source_indices[index])
            area_by_index[source_indices[index]] = areas[index]

    return protected, area_by_index


def _set_attribute(tag: bytes, pattern: re.Pattern, name: bytes, value: Optional[str]) -> bytes:
    match = pattern.search(tag)
    if value is None:
        return pattern.sub(b"", tag, count=1) if match else tag
    encoded = value.encode("ascii")
    if match:
        start, end = match.span("value")
        return tag[:start] + encoded + tag[end:]

    close = tag.rfind(b"/>")
    if close < 0:
        close = tag.rfind(b">")
    if close < 0:
        raise ConversionError("Malformed <triangle> tag")
    return tag[:close] + b" " + name + b'="' + encoded + b'"' + tag[close:]


def convert_model_xml(
    data: bytes,
    *,
    slots: Optional[Set[int]] = None,
    remove_colour: bool = False,
    protect_planar: bool = False,
    planar_angle_degrees: float = 2.0,
    planar_minimum_area_mm2: float = 20.0,
    planar_compact_strip_minimum_area_mm2: Optional[float] = None,
    planar_coplanar_lane_minimum_patch_area_mm2: Optional[float] = None,
    planar_coplanar_lane_axis: Optional[
        Tuple[float, float, float] | Sequence[Tuple[float, float, float]]
    ] = None,
    planar_layered_feature_maximum_depth_mm: Optional[float] = None,
    protect_connected_coplanar: bool = False,
    connected_coplanar_angle_degrees: float = 0.5,
    connected_coplanar_plane_tolerance_mm: float = 0.02,
    connected_coplanar_minimum_short_span_mm: float = 0.0,
    connected_coplanar_minimum_long_span_mm: float = 0.0,
    connected_coplanar_satellite_gap_mm: float = 0.0,
    protect_flat_contact: bool = False,
    protect_fuzz_envelope: bool = False,
    fuzz_envelope_distance_mm: float = 0.25,
    fuzz_envelope_contact_neighbor_rings: int = 1,
    protect_overlap: bool = True,
    protect_nonmanifold: bool = True,
    protect_coincident: bool = False,
    report: Optional[ConversionReport] = None,
) -> Tuple[bytes, bool]:
    """Convert colour attributes in one 3MF model XML member."""
    report = report or ConversionReport()
    changed = False

    def convert_triangle_tags(
        xml: bytes,
        planar_protected: Set[int],
        flat_contact_protected: dict[int, float],
        fuzz_envelope_protected: dict[int, float],
        overlap_protected: dict[int, float],
        nonmanifold_protected: dict[int, float],
        coincident_protected: dict[int, float],
    ) -> bytes:
        triangle_index = -1

        def replace_tag(match: re.Match) -> bytes:
            nonlocal changed, triangle_index
            triangle_index += 1
            tag = match.group(0)
            report.triangles_scanned += 1
            colour_match = COLOR_ATTR_RE.search(tag)
            if not colour_match:
                return tag

            report.colour_triangles += 1
            raw = colour_match.group("value").decode("ascii")
            tree = decode_paint_stream(raw)
            states = list(leaf_states(tree))
            for state in states:
                if state > 0 and (slots is None or state in slots):
                    report.selected_leaf_counts[state] += 1

            if triangle_index in flat_contact_protected:
                report.flat_contact_triangles_protected += 1
                report.flat_contact_area_protected_mm2 += flat_contact_protected[triangle_index]
                return tag

            if triangle_index in fuzz_envelope_protected:
                report.fuzz_envelope_triangles_protected += 1
                report.fuzz_envelope_area_protected_mm2 += fuzz_envelope_protected[triangle_index]
                return tag

            if triangle_index in overlap_protected:
                report.overlap_triangles_protected += 1
                report.overlap_area_protected_mm2 += overlap_protected[triangle_index]
                return tag

            if triangle_index in nonmanifold_protected:
                report.nonmanifold_triangles_protected += 1
                report.nonmanifold_area_protected_mm2 += nonmanifold_protected[triangle_index]
                return tag

            if triangle_index in coincident_protected:
                report.coincident_triangles_protected += 1
                report.coincident_area_protected_mm2 += coincident_protected[triangle_index]
                return tag

            if triangle_index in planar_protected:
                report.planar_triangles_protected += 1
                return tag

            fuzzy_tree = colour_tree_to_fuzzy(tree, slots)
            has_fuzzy = any(state == 1 for state in leaf_states(fuzzy_tree))
            new_tag = tag
            if has_fuzzy:
                new_tag = _set_attribute(
                    new_tag, FUZZY_ATTR_RE, b"paint_fuzzy_skin", encode_paint_stream(fuzzy_tree)
                )
                report.fuzzy_triangles_written += 1
            else:
                report.skipped_colour_triangles += 1

            if remove_colour:
                remaining_tree = remove_selected_colours(tree, slots)
                if any(state > 0 for state in leaf_states(remaining_tree)):
                    new_tag = _set_attribute(
                        new_tag, COLOR_ATTR_RE, b"paint_color", encode_paint_stream(remaining_tree)
                    )
                    report.colour_attributes_updated += 1
                else:
                    new_tag = _set_attribute(new_tag, COLOR_ATTR_RE, b"paint_color", None)
                    report.colour_attributes_removed += 1

            if new_tag != tag:
                changed = True
            return new_tag

        return TRIANGLE_RE.sub(replace_tag, xml)

    if (
        not protect_planar
        and not protect_connected_coplanar
        and not protect_flat_contact
        and not protect_fuzz_envelope
        and not protect_overlap
        and not protect_nonmanifold
        and not protect_coincident
    ):
        return convert_triangle_tags(data, set(), {}, {}, {}, {}, {}), changed

    found_object = False

    def replace_object(match: re.Match) -> bytes:
        nonlocal found_object
        found_object = True
        object_xml = match.group(0)
        use_rust_mesh_protection = bool(
            _rust_engine is not None
            and protect_flat_contact
            and protect_fuzz_envelope
        )
        use_rust_full_mesh_protection = bool(
            use_rust_mesh_protection
            and hasattr(_rust_engine, "find_mesh_protection")
            and (protect_planar or protect_connected_coplanar)
            and planar_coplanar_lane_minimum_patch_area_mm2 is None
            and planar_coplanar_lane_axis is None
            and planar_layered_feature_maximum_depth_mm is None
        )
        shared_geometry = (
            _parse_mesh_geometry(object_xml, slots)
            if (
                (
                    (protect_planar or protect_connected_coplanar)
                    and not use_rust_full_mesh_protection
                )
                or (
                    not use_rust_mesh_protection
                    and (protect_flat_contact or protect_fuzz_envelope)
                )
            )
            else None
        )
        planar_protected: Set[int] = set()
        flat_contact_protected: dict[int, float] = {}
        fuzz_envelope_protected: dict[int, float] = {}
        if use_rust_full_mesh_protection:
            (
                rust_planar,
                patches,
                area,
                rust_contacts,
                rust_envelope,
            ) = _rust_engine.find_mesh_protection(
                object_xml,
                sorted(slots) if slots is not None else None,
                angle_degrees=planar_angle_degrees,
                minimum_area_mm2=planar_minimum_area_mm2,
                compact_strip_minimum_area_mm2=planar_compact_strip_minimum_area_mm2,
                protect_connected_coplanar=protect_connected_coplanar,
                connected_coplanar_angle_degrees=connected_coplanar_angle_degrees,
                connected_coplanar_plane_tolerance_mm=connected_coplanar_plane_tolerance_mm,
                connected_coplanar_minimum_short_span_mm=connected_coplanar_minimum_short_span_mm,
                connected_coplanar_minimum_long_span_mm=connected_coplanar_minimum_long_span_mm,
                connected_coplanar_satellite_gap_mm=connected_coplanar_satellite_gap_mm,
                distance_mm=fuzz_envelope_distance_mm,
                contact_neighbor_rings=fuzz_envelope_contact_neighbor_rings,
            )
            planar_protected = set(rust_planar)
            flat_contact_protected = dict(rust_contacts)
            fuzz_envelope_protected = dict(rust_envelope)
            report.planar_patches_protected += patches
            report.planar_area_protected_mm2 += area
        elif protect_planar or protect_connected_coplanar:
            planar_protected, patches, area = _find_planar_protection(
                object_xml,
                slots,
                angle_degrees=planar_angle_degrees,
                minimum_area_mm2=planar_minimum_area_mm2,
                compact_strip_minimum_area_mm2=planar_compact_strip_minimum_area_mm2,
                coplanar_lane_minimum_patch_area_mm2=planar_coplanar_lane_minimum_patch_area_mm2,
                coplanar_lane_axis=planar_coplanar_lane_axis,
                layered_feature_maximum_depth_mm=planar_layered_feature_maximum_depth_mm,
                protect_connected_coplanar=protect_connected_coplanar,
                connected_coplanar_angle_degrees=connected_coplanar_angle_degrees,
                connected_coplanar_plane_tolerance_mm=connected_coplanar_plane_tolerance_mm,
                connected_coplanar_minimum_short_span_mm=connected_coplanar_minimum_short_span_mm,
                connected_coplanar_minimum_long_span_mm=connected_coplanar_minimum_long_span_mm,
                connected_coplanar_satellite_gap_mm=connected_coplanar_satellite_gap_mm,
                geometry=shared_geometry,
            )
            report.planar_patches_protected += patches
            report.planar_area_protected_mm2 += area
        if use_rust_mesh_protection and not use_rust_full_mesh_protection:
            rust_contacts, rust_envelope = (
                _rust_engine.find_flat_contact_and_fuzz_envelope(
                    object_xml,
                    sorted(slots) if slots is not None else None,
                    fuzz_envelope_distance_mm,
                    fuzz_envelope_contact_neighbor_rings,
                )
            )
            flat_contact_protected = dict(rust_contacts)
            fuzz_envelope_protected = dict(rust_envelope)
        elif protect_flat_contact and not use_rust_full_mesh_protection:
            _flat_contact_indices, flat_contact_protected = _find_flat_contact_protection(
                object_xml, slots, geometry=shared_geometry
            )
        if protect_fuzz_envelope and not use_rust_mesh_protection:
            _fuzz_envelope_indices, fuzz_envelope_protected = _find_fuzz_envelope_protection(
                object_xml,
                slots,
                distance_mm=fuzz_envelope_distance_mm,
                contact_seed_indices=_flat_contact_indices if protect_flat_contact else set(),
                contact_neighbor_rings=fuzz_envelope_contact_neighbor_rings,
                geometry=shared_geometry,
            )
        overlap_protected: dict[int, float] = {}
        if protect_overlap:
            _overlap_indices, overlap_protected = _find_overlap_protection(object_xml)
        nonmanifold_protected: dict[int, float] = {}
        if protect_nonmanifold:
            _nonmanifold_indices, nonmanifold_protected = _find_nonmanifold_protection(object_xml)
        coincident_protected: dict[int, float] = {}
        if protect_coincident:
            _coincident_indices, coincident_protected = _find_coincident_protection(object_xml)
        return convert_triangle_tags(
            object_xml,
            planar_protected,
            flat_contact_protected,
            fuzz_envelope_protected,
            overlap_protected,
            nonmanifold_protected,
            coincident_protected,
        )

    converted = OBJECT_RE.sub(replace_object, data)
    if not found_object:
        converted = convert_triangle_tags(data, set(), {}, {}, {}, {}, {})
    return converted, changed


def _validate_slots(slots: Optional[Iterable[int]]) -> Optional[Set[int]]:
    if slots is None:
        return None
    result = {int(slot) for slot in slots}
    if not result or any(slot < 1 or slot > 255 for slot in result):
        raise ConversionError("Filament slots must be integers from 1 to 255")
    return result


def _configure_fuzzy_settings(data: bytes, settings: dict[str, object]) -> bytes:
    """Update Bambu's fuzzy-skin process settings without disturbing other settings."""
    try:
        project = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("Could not read metadata/project_settings.config") from exc
    if not isinstance(project, dict):
        raise ConversionError("metadata/project_settings.config is not a JSON object")
    allowed = {
        "fuzzy_skin_first_layer",
        "fuzzy_skin_mode",
        "fuzzy_skin_noise_type",
        "fuzzy_skin_octaves",
        "fuzzy_skin_persistence",
        "fuzzy_skin_point_distance",
        "fuzzy_skin_scale",
        "fuzzy_skin_thickness",
    }
    for key, value in settings.items():
        if key not in allowed:
            raise ConversionError(f"Unsupported fuzzy-skin setting: {key}")
        project[key] = str(value)
    # Bambu does not treat values stored in project_settings.config as custom
    # merely because they differ from the selected process preset.  If their
    # keys are absent here, opening the project in the GUI silently replaces
    # them with the preset defaults (normally 0.30 / 0.80).  Register every
    # fuzzy value written by FuzzyPaint so GUI and headless slicing agree.
    _register_project_overrides(project, process_keys=settings.keys())
    return json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _register_project_overrides(
    project: dict, *, process_keys: Iterable[str] = (), filament_keys: Iterable[str] = ()
) -> None:
    """Tell Bambu that stored values intentionally override named presets.

    Project settings contain one difference-list entry for the process, one
    per filament, and one for the printer.  Merely changing a value without
    registering its key makes Bambu silently restore the system-preset value.
    """
    filament_ids = project.get("filament_settings_id")
    filament_count = len(filament_ids) if isinstance(filament_ids, list) else 0
    required = filament_count + 2
    current = project.get("different_settings_to_system")
    differences = list(current) if isinstance(current, list) else []
    if len(differences) < required:
        differences.extend([""] * (required - len(differences)))

    def merge(index: int, keys: Iterable[str]) -> None:
        existing = {item for item in str(differences[index]).split(";") if item}
        existing.update(keys)
        differences[index] = ";".join(sorted(existing))

    merge(0, process_keys)
    for index in range(1, filament_count + 1):
        merge(index, filament_keys)
    project["different_settings_to_system"] = differences


def _configure_wipe_while_retracting(data: bytes, disabled: bool) -> bytes:
    """Disable ordinary model wipes while preserving all other profile values.

    Bambu stores a process-level fallback in ``wipe`` and optional per-filament
    overrides in ``filament_wipe``.  Set both so every colour used to generate
    the multicolour toolpaths is sliced consistently.  Retraction length,
    speed, Z-hop and wipe distance are deliberately left untouched.
    """
    if not disabled:
        return data
    try:
        project = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("Could not read metadata/project_settings.config") from exc
    if not isinstance(project, dict):
        raise ConversionError("metadata/project_settings.config is not a JSON object")

    def disabled_value(current):
        if isinstance(current, list):
            return ["0"] * len(current)
        return "0"

    project["wipe"] = disabled_value(project.get("wipe", "0"))
    if "filament_wipe" in project:
        project["filament_wipe"] = disabled_value(project["filament_wipe"])
    _register_project_overrides(
        project, process_keys=("wipe",), filament_keys=("filament_wipe",)
    )
    return json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _configure_retractions(data: bytes, disabled: bool) -> bytes:
    """Disable ordinary model retractions for every virtual filament.

    Bambu stores a process fallback plus optional per-filament overrides.  Both
    must be zeroed before slicing; changing only the first visible filament can
    leave retractions in toolpaths generated for the other painted colour.
    Tool-change retractions are left alone because their complete CP blocks are
    removed during the single-filament collapse step.
    """
    if not disabled:
        return data
    try:
        project = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("Could not read metadata/project_settings.config") from exc
    if not isinstance(project, dict):
        raise ConversionError("metadata/project_settings.config is not a JSON object")

    def zero_value(current):
        if isinstance(current, list):
            return ["0"] * len(current)
        return "0"

    project["retraction_length"] = zero_value(project.get("retraction_length", "0"))
    if "filament_retraction_length" in project:
        project["filament_retraction_length"] = zero_value(
            project["filament_retraction_length"]
        )
    # Firmware-managed G10/G11 retractions would bypass the zero lengths.
    project["use_firmware_retraction"] = "0"
    _register_project_overrides(
        project,
        process_keys=("retraction_length", "use_firmware_retraction"),
        filament_keys=("filament_retraction_length",),
    )
    return json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _configure_sloped_z_hop(data: bytes, enabled: bool) -> bytes:
    """Select Bambu's sloped lift without changing any Z-hop heights."""
    if not enabled:
        return data
    try:
        project = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("Could not read metadata/project_settings.config") from exc
    if not isinstance(project, dict):
        raise ConversionError("metadata/project_settings.config is not a JSON object")

    def sloped_value(current):
        if isinstance(current, list):
            return ["Slope Lift"] * len(current)
        return "Slope Lift"

    project["z_hop_types"] = sloped_value(project.get("z_hop_types", "Auto Lift"))
    if "filament_z_hop_types" in project:
        project["filament_z_hop_types"] = sloped_value(project["filament_z_hop_types"])
    _register_project_overrides(
        project,
        process_keys=("z_hop_types",),
        filament_keys=("filament_z_hop_types",),
    )
    return json.dumps(project, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def configure_project_for_slicing(
    source: Path | str,
    destination: Path | str,
    *,
    disable_retractions: bool = False,
    disable_wipe_while_retracting: bool = False,
    force_sloped_z_hop: bool = False,
) -> None:
    """Copy an editable 3MF while enforcing last-minute slicing settings.

    This deliberately avoids touching meshes or paint masks.  It lets the GUI
    apply a slicing-only choice to an existing prepared project without making
    the user repeat the expensive colour-to-fuzzy preparation stage.
    """
    source_path = Path(source)
    destination_path = Path(destination)
    if source_path == destination_path:
        raise ConversionError("Choose a different temporary slicing file")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with zipfile.ZipFile(source_path, "r") as source_zip:
            infos = source_zip.infolist()
            if any(info.filename.lower().endswith(".gcode") for info in infos):
                raise ConversionError("Slicing settings can only be changed on an editable 3MF project")
            settings_name = next(
                (
                    info.filename
                    for info in infos
                    if info.filename.lower() == "metadata/project_settings.config"
                ),
                None,
            )
            if settings_name is None:
                raise ConversionError("The project contains no Bambu project settings")
            fd, temp_name = tempfile.mkstemp(
                prefix=destination_path.stem + "-", suffix=".tmp", dir=destination_path.parent
            )
            os.close(fd)
            with zipfile.ZipFile(temp_name, "w", allowZip64=True) as output_zip:
                output_zip.comment = source_zip.comment
                for info in infos:
                    payload = source_zip.read(info)
                    if info.filename == settings_name:
                        payload = _configure_retractions(payload, disable_retractions)
                        payload = _configure_wipe_while_retracting(payload, disable_wipe_while_retracting)
                        payload = _configure_sloped_z_hop(payload, force_sloped_z_hop)
                    output_zip.writestr(info, payload)
        os.replace(temp_name, destination_path)
        temp_name = None
    except zipfile.BadZipFile as exc:
        raise ConversionError("The input is not a valid 3MF/ZIP project") from exc
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def convert_3mf(
    source: os.PathLike | str,
    destination: os.PathLike | str,
    *,
    slots: Optional[Iterable[int]] = None,
    remove_colour: bool = False,
    protect_planar: bool = False,
    planar_angle_degrees: float = 2.0,
    planar_minimum_area_mm2: float = 20.0,
    planar_compact_strip_minimum_area_mm2: Optional[float] = None,
    planar_coplanar_lane_minimum_patch_area_mm2: Optional[float] = None,
    planar_coplanar_lane_axis: Optional[
        Tuple[float, float, float] | Sequence[Tuple[float, float, float]]
    ] = None,
    planar_layered_feature_maximum_depth_mm: Optional[float] = None,
    protect_connected_coplanar: bool = False,
    connected_coplanar_angle_degrees: float = 0.5,
    connected_coplanar_plane_tolerance_mm: float = 0.02,
    connected_coplanar_minimum_short_span_mm: float = 0.0,
    connected_coplanar_minimum_long_span_mm: float = 0.0,
    connected_coplanar_satellite_gap_mm: float = 0.0,
    protect_flat_contact: bool = False,
    protect_fuzz_envelope: bool = False,
    fuzz_envelope_distance_mm: float = 0.25,
    fuzz_envelope_contact_neighbor_rings: int = 1,
    protect_overlap: bool = True,
    protect_nonmanifold: bool = True,
    protect_coincident: bool = False,
    single_colour: bool = False,
    single_colour_slot: int = 1,
    single_spool_segmented: bool = False,
    fuzzy_settings: Optional[dict[str, object]] = None,
    disable_wipe_while_retracting: bool = False,
    disable_retractions: bool = False,
    force_sloped_z_hop: bool = False,
    strip_nonprint_auxiliaries: bool = False,
) -> ConversionReport:
    """Write a converted copy of a Bambu Studio 3MF project."""
    source_path = Path(source).expanduser().resolve()
    destination_path = Path(destination).expanduser().resolve()
    selected_slots = _validate_slots(slots)
    if single_colour_slot < 1 or single_colour_slot > 255:
        raise ConversionError("The single-colour filament slot must be from 1 to 255")
    if single_colour and single_spool_segmented:
        raise ConversionError("Single-colour and single-spool segmented modes are mutually exclusive")
    if single_spool_segmented and selected_slots is None:
        raise ConversionError("Single-spool segmented mode requires explicit filament slots")

    if source_path == destination_path:
        raise ConversionError("Choose a different output file; the source is never overwritten")
    if not source_path.is_file():
        raise ConversionError(f"Input file does not exist: {source_path}")
    if source_path.suffix.lower() != ".3mf":
        raise ConversionError("Input must be a .3mf project saved by Bambu Studio or OrcaSlicer")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    report = ConversionReport()
    temp_name = None
    try:
        with zipfile.ZipFile(source_path, "r") as source_zip:
            # Reading retained members below performs CRC validation.  When
            # non-print auxiliaries are intentionally omitted, do not spend
            # time decompressing large manuals/creation-kit archives merely
            # to test data that will not be copied.
            if not strip_nonprint_auxiliaries:
                bad_member = source_zip.testzip()
                if bad_member:
                    raise ConversionError(f"The 3MF archive is corrupt at: {bad_member}")
            infos = source_zip.infolist()
            report.archive_entries = len(infos)
            contains_sliced_gcode = any(
                info.filename.lower().endswith(".gcode") for info in infos
            )
            if not any(info.filename.lower().endswith(".model") for info in infos):
                raise ConversionError("This archive contains no 3MF .model files")

            fd, temp_name = tempfile.mkstemp(
                prefix=destination_path.stem + "-", suffix=".tmp", dir=destination_path.parent
            )
            os.close(fd)
            with zipfile.ZipFile(temp_name, "w", allowZip64=True) as output_zip:
                output_zip.comment = source_zip.comment
                for info in infos:
                    lower_name = info.filename.lower()
                    if (
                        strip_nonprint_auxiliaries
                        and lower_name.startswith("auxiliaries/")
                        and not lower_name.startswith("auxiliaries/.thumbnails/")
                    ):
                        report.attachments_removed += 1
                        report.attachment_bytes_removed += info.file_size
                        continue
                    payload = source_zip.read(info)
                    if lower_name.endswith(".model"):
                        report.model_files += 1
                        payload, modified = convert_model_xml(
                            payload,
                            slots=selected_slots,
                            remove_colour=remove_colour,
                            protect_planar=protect_planar,
                            planar_angle_degrees=planar_angle_degrees,
                            planar_minimum_area_mm2=planar_minimum_area_mm2,
                            planar_compact_strip_minimum_area_mm2=planar_compact_strip_minimum_area_mm2,
                            planar_coplanar_lane_minimum_patch_area_mm2=planar_coplanar_lane_minimum_patch_area_mm2,
                            planar_coplanar_lane_axis=planar_coplanar_lane_axis,
                            planar_layered_feature_maximum_depth_mm=planar_layered_feature_maximum_depth_mm,
                            protect_connected_coplanar=protect_connected_coplanar,
                            connected_coplanar_angle_degrees=connected_coplanar_angle_degrees,
                            connected_coplanar_plane_tolerance_mm=connected_coplanar_plane_tolerance_mm,
                            connected_coplanar_minimum_short_span_mm=connected_coplanar_minimum_short_span_mm,
                            connected_coplanar_minimum_long_span_mm=connected_coplanar_minimum_long_span_mm,
                            connected_coplanar_satellite_gap_mm=connected_coplanar_satellite_gap_mm,
                            protect_flat_contact=protect_flat_contact,
                            protect_fuzz_envelope=protect_fuzz_envelope,
                            fuzz_envelope_distance_mm=fuzz_envelope_distance_mm,
                            fuzz_envelope_contact_neighbor_rings=fuzz_envelope_contact_neighbor_rings,
                            protect_overlap=protect_overlap,
                            protect_nonmanifold=protect_nonmanifold,
                            protect_coincident=protect_coincident,
                            report=report,
                        )
                        report.model_files_modified += int(modified)
                        if single_colour:
                            payload, removed = _strip_all_colour_paint(payload)
                            report.single_colour_attributes_removed += removed
                    elif single_colour and lower_name == "metadata/model_settings.config":
                        payload, remapped = _remap_extruder_metadata(
                            payload, single_colour_slot
                        )
                        report.single_colour_assignments_remapped += remapped
                    elif lower_name == "metadata/project_settings.config":
                        if single_spool_segmented:
                            payload, pairs = _configure_single_spool_segmentation(
                                payload, selected_slots or set(), single_colour_slot
                            )
                            report.single_spool_pairs_zeroed += pairs
                        if fuzzy_settings:
                            payload = _configure_fuzzy_settings(payload, fuzzy_settings)
                        if disable_wipe_while_retracting:
                            payload = _configure_wipe_while_retracting(payload, True)
                            report.wipe_while_retracting_disabled = True
                        if disable_retractions:
                            payload = _configure_retractions(payload, True)
                            report.retractions_disabled = True
                        if force_sloped_z_hop:
                            payload = _configure_sloped_z_hop(payload, True)
                            report.sloped_z_hop_enabled = True
                    output_zip.writestr(info, payload)

        if report.colour_triangles == 0:
            if contains_sliced_gcode:
                raise ConversionError(
                    "This is an already-sliced .gcode.3mf file. Step 1 needs the "
                    "unsliced Bambu Studio project; use sliced files in Step 2."
                )
            raise ConversionError("No Bambu paint_color data was found in this project")
        if report.fuzzy_triangles_written == 0:
            requested = "all slots" if selected_slots is None else f"slot(s) {sorted(selected_slots)}"
            raise ConversionError(f"No painted areas matched {requested}")
        os.replace(temp_name, destination_path)
        temp_name = None
        return report
    except zipfile.BadZipFile as exc:
        raise ConversionError("The input is not a valid 3MF/ZIP project") from exc
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def parse_slots(text: str) -> Optional[Set[int]]:
    cleaned = text.strip().lower()
    if cleaned in {"", "all", "*"}:
        return None
    try:
        return _validate_slots(int(part.strip()) for part in cleaned.split(",") if part.strip())
    except ValueError as exc:
        raise ConversionError("Use 'all' or comma-separated slot numbers, for example: 2,4") from exc


def clean_sliced_output_base(filename: str) -> str:
    """Build a tidy base name from a Bambu plate-sliced package name.

    Bambu Studio appends the plate name after an underscore.  If that same
    multi-word name is already present in the project name, remove only the
    duplicated trailing copy from FuzzyPaint's suggested output name.
    """
    name = Path(filename).name
    base = name[:-10] if name.lower().endswith(".gcode.3mf") else Path(name).stem
    if "_" not in base:
        return base

    prefix, suffix = base.rsplit("_", 1)

    def normalized(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9]+", value.lower()))

    suffix_words = normalized(suffix)
    prefix_words = normalized(prefix)
    if len(suffix_words.split()) >= 2 and f" {suffix_words} " in f" {prefix_words} ":
        return prefix
    return base


# Stage-suffix naming: the "C2F-" prefix is the tool's old Colour2Fuzz name;
# the regexes below still recognise it purely so that re-running FuzzyPaint on
# an older file strips the old suffix instead of appending a second one. Only
# "FP-" is ever generated going forward.
_STAGE_SUFFIX_RE = r"(?:C2F|FP)-(?:Prepped|Created|Print(?:_Ready)?)"

_TRAILING_VERSION_RE = re.compile(r"\s+v(\d+)$")


def next_available_destination(path: Path) -> Path:
    """If `path` already exists, return the next free "... vN" variant instead.

    Numbering starts at v2 -- the plain, unversioned name is implicitly "v1" --
    and uses plain integers (v2, v3, v4, ...), not a dotted v1.1/v1.2 scheme.
    A name that already ends in " vN" has that stripped first, so repeatedly
    hitting a collision advances the number instead of stacking suffixes.
    """
    if not path.exists():
        return path
    name = path.name
    if name.lower().endswith(".gcode.3mf"):
        stem, ext = name[:-10], name[-10:]
    else:
        stem, ext = path.stem, path.suffix
    stem = _TRAILING_VERSION_RE.sub("", stem)
    counter = 2
    while True:
        candidate = path.with_name(f"{stem} v{counter}{ext}")
        if not candidate.exists():
            return candidate
        counter += 1


def suggest_prepared_project_name(filename: str) -> str:
    """Suggest the canonical stage-one project filename."""
    stem = Path(filename).stem
    already_prepped = bool(re.search(r"\s*\((?:C2F|FP)-Prepped(?: v\d+)?\)$", stem, re.IGNORECASE))
    base = re.sub(rf"\s*\({_STAGE_SUFFIX_RE}(?: v\d+)?\)$", "", stem, flags=re.IGNORECASE)
    base = re.sub(r"-(?:prepared-fuzzy|prepared)$", "", base, flags=re.IGNORECASE)
    suffix = " (FP-Prepped v2)" if already_prepped else " (FP-Prepped)"
    return base + suffix + ".3mf"


def suggest_created_package_name(filename: str) -> str:
    """Suggest the filename to type into Bambu Studio's sliced-file export."""
    stem = Path(filename).stem
    base = re.sub(rf"\s*\({_STAGE_SUFFIX_RE}(?: v\d+)?\)$", "", stem, flags=re.IGNORECASE)
    base = re.sub(r"-(?:prepared-fuzzy|prepared)$", "", base, flags=re.IGNORECASE)
    return base + " (FP-Created).gcode.3mf"


def suggest_print_package_name(filename: str) -> str:
    """Suggest the canonical final printable package filename."""
    name = Path(filename).name
    stem = name[:-10] if name.lower().endswith(".gcode.3mf") else Path(name).stem
    base = re.sub(
        rf"\s*\({_STAGE_SUFFIX_RE}(?: v\d+)?\)(?:_[^()]*)?$",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    if base == stem:
        base = re.sub(
            r"-(?:prepared-fuzzy|prepared)(?:_[^()]*)?$",
            "",
            clean_sliced_output_base(name),
            flags=re.IGNORECASE,
        )
    return base + " (FP-Print_Ready).gcode.3mf"


def format_report(report: ConversionReport) -> str:
    slots = ", ".join(
        f"{slot} ({count} leaf region{'s' if count != 1 else ''})"
        for slot, count in sorted(report.selected_leaf_counts.items())
    )
    return (
        f"Converted {report.fuzzy_triangles_written:,} painted triangle(s) in "
        f"{report.model_files_modified:,} model file(s)."
        + (f"\nMatched filament slots: {slots}." if slots else "")
        + (
            f"\nProtected {report.planar_patches_protected:,} large flat patch(es) "
            f"({report.planar_triangles_protected:,} triangle(s), "
            f"{report.planar_area_protected_mm2:,.1f} mm²)."
            if report.planar_patches_protected
            else ""
        )
        + (
            f"\nSkipped fuzzing {report.flat_contact_triangles_protected:,} triangle(s) "
            f"({report.flat_contact_area_protected_mm2:,.1f} mm²) whose faces lie flat "
            f"against another surface."
            if report.flat_contact_triangles_protected
            else ""
        )
        + (
            f"\nSkipped fuzzing {report.fuzz_envelope_triangles_protected:,} triangle(s) "
            f"({report.fuzz_envelope_area_protected_mm2:,.1f} mm²) whose fuzzy "
            f"displacement could intersect another surface."
            if report.fuzz_envelope_triangles_protected
            else ""
        )
        + (
            f"\nSkipped fuzzing {report.overlap_triangles_protected:,} triangle(s) "
            f"({report.overlap_area_protected_mm2:,.1f} mm²) found sitting flush against "
            f"disconnected/overlapping geometry."
            if report.overlap_triangles_protected
            else ""
        )
        + (
            f"\nSkipped fuzzing {report.nonmanifold_triangles_protected:,} triangle(s) "
            f"({report.nonmanifold_area_protected_mm2:,.1f} mm²) found touching a "
            f"non-manifold mesh edge."
            if report.nonmanifold_triangles_protected
            else ""
        )
        + (
            f"\nSkipped fuzzing {report.coincident_triangles_protected:,} triangle(s) "
            f"({report.coincident_area_protected_mm2:,.1f} mm²) found coincident with "
            f"another piece's flat surface."
            if report.coincident_triangles_protected
            else ""
        )
        + (
            f"\nRemoved {report.attachments_removed:,} embedded guide/media file(s) "
            f"({report.attachment_bytes_removed / 1_000_000:.1f} MB)."
            if report.attachments_removed
            else ""
        )
        + (
            ""
            if report.single_colour_attributes_removed
            else (
                f"\nRemoved converted colour paint from {report.colour_attributes_removed + report.colour_attributes_updated:,} triangle(s)."
                if report.colour_attributes_removed or report.colour_attributes_updated
                else "\nKept the original colour painting."
            )
        )
        + (
            f"\nSingle-colour output: removed {report.single_colour_attributes_removed:,} "
            f"face-colour attribute(s) and remapped "
            f"{report.single_colour_assignments_remapped:,} part assignment(s)."
            if report.single_colour_attributes_removed or report.single_colour_assignments_remapped
            else ""
        )
        + (
            f"\nSingle-spool segmented output: disabled the prime tower and zeroed "
            f"{report.single_spool_pairs_zeroed:,} directional flush pair(s)."
            if report.single_spool_pairs_zeroed
            else ""
        )
        + (
            "\nDisabled wipe while retracting for the prepared project."
            if report.wipe_while_retracting_disabled
            else ""
        )
        + (
            "\nDisabled ordinary model retractions for every filament."
            if report.retractions_disabled
            else ""
        )
        + (
            "\nSelected sloped Z-hop for the prepared project."
            if report.sloped_z_hop_enabled
            else ""
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Convert Bambu Studio colour painting into fuzzy-skin painting."
    )
    parser.add_argument("input", type=Path, help="painted Bambu/Orca .3mf project")
    parser.add_argument("output", type=Path, help="new converted .3mf project")
    parser.add_argument(
        "--slots", default="all", help="filament slots to convert, e.g. 2,4 (default: all)"
    )
    parser.add_argument(
        "--remove-colour",
        action="store_true",
        help="remove converted colour painting (default: retain colour and add fuzzy skin)",
    )
    parser.add_argument(
        "--protect-planar",
        action="store_true",
        help="do not add fuzzy skin to connected flat painted patches of at least 20 mm²",
    )
    parser.add_argument(
        "--no-protect-overlap",
        dest="protect_overlap",
        action="store_false",
        help="also fuzz triangles sitting flush against disconnected/overlapping geometry "
        "(default: skip fuzzing them, since they are mesh debris rather than real paint)",
    )
    parser.add_argument(
        "--no-protect-nonmanifold",
        dest="protect_nonmanifold",
        action="store_false",
        help="also fuzz triangles touching a non-manifold mesh edge "
        "(default: skip fuzzing them, since they are mesh debris rather than real paint)",
    )
    parser.add_argument(
        "--protect-coincident",
        action="store_true",
        help="do not fuzz flat surfaces that sit coincident with another piece's flat surface "
        "(default: off -- validated well on a hand-picked test case, but over-fires on complex "
        "models with many legitimately-touching mechanical parts; opt in and check the result)",
    )
    parser.add_argument(
        "--single-colour",
        action="store_true",
        help="retain fuzzy skin but remove face colours and remap every part to filament 1",
    )
    parser.add_argument(
        "--single-spool-segmented",
        action="store_true",
        help="retain virtual colour-region outlines but disable purge/tower between same-spool slots",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = convert_3mf(
            args.input,
            args.output,
            slots=parse_slots(args.slots),
            remove_colour=args.remove_colour,
            protect_planar=args.protect_planar,
            protect_overlap=args.protect_overlap,
            protect_nonmanifold=args.protect_nonmanifold,
            protect_coincident=args.protect_coincident,
            single_colour=args.single_colour,
            single_spool_segmented=args.single_spool_segmented,
        )
    except (ConversionError, OSError) as exc:
        print(f"Error: {exc}")
        return 1
    print(format_report(report))
    print(f"Saved: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
