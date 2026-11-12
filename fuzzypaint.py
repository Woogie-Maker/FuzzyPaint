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


def _find_planar_protection(
    object_xml: bytes,
    slots: Optional[Set[int]],
    *,
    angle_degrees: float,
    minimum_area_mm2: float,
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

    A second pass then absorbs small leftover patches that sit right next
    to a patch large enough to already be protected on its own. Some
    models carry genuine small physical steps within an otherwise flat
    area -- e.g. the faint outline of a boolean cut that didn't go quite
    deep enough -- which no amount of angle-tolerance tuning should bridge,
    because the step is real, not mesh noise. But a human looking at the
    surface still reads the whole thing as "the flat area, with a rough
    spot," not as a separate feature worth keeping fuzzy, and a few
    unfuzzed triangles sitting inside a fuzzed field of equally-real
    paint-job and tool-mark bumps would look far more out of place than
    just smoothing over the whole patch. Any patch below the area
    threshold that shares a geometric edge with an admitted patch (directly
    or via a chain of other small patches) is absorbed into protection too,
    regardless of its own normal. This can occasionally over-absorb if a
    long chain of small patches happens to connect a flat area to a
    genuinely different, separate feature -- accepted as a reasonable
    trade-off given what it fixes.
    """
    vertices = []
    for match in VERTEX_RE.finditer(object_xml):
        tag = match.group(0)
        coords = [_simple_attribute(tag, axis) for axis in (b"x", b"y", b"z")]
        if any(value is None for value in coords):
            return set(), 0, 0.0
        vertices.append(tuple(float(value) for value in coords))
    if not vertices:
        return set(), 0, 0.0

    def position_key(index: int) -> Tuple[float, float, float]:
        return tuple(round(value, 4) for value in vertices[index])

    areas = []
    normals = []
    source_indices = []
    adjacency: List[List[int]] = []
    edge_owner = {}
    cosine_limit = math.cos(math.radians(angle_degrees))

    for triangle_index, match in enumerate(TRIANGLE_RE.finditer(object_xml)):
        tag = match.group(0)
        if not _selected_colour_in_tag(tag, slots):
            continue
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
        normal = tuple(value / length for value in cross)
        candidate = len(areas)
        areas.append(length * 0.5)
        normals.append(normal)
        source_indices.append(triangle_index)
        adjacency.append([])

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
    patches: List[Tuple[List[int], float]] = []
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
        patches.append((members, sum(areas[member] for member in members)))

    # Absorb small patches that touch an already-large-enough patch (see the
    # docstring). Build a patch-level adjacency graph from the same raw,
    # angle-independent edge adjacency used to grow the patches above, then
    # flood out from every patch that clears the area threshold on its own.
    patch_of_candidate = [0] * len(areas)
    for patch_index, (members, _total_area) in enumerate(patches):
        for member in members:
            patch_of_candidate[member] = patch_index

    patch_neighbours: List[Set[int]] = [set() for _ in patches]
    for candidate, neighbours in enumerate(adjacency):
        this_patch = patch_of_candidate[candidate]
        for neighbour in neighbours:
            other_patch = patch_of_candidate[neighbour]
            if other_patch != this_patch:
                patch_neighbours[this_patch].add(other_patch)
                patch_neighbours[other_patch].add(this_patch)

    admitted = [total_area >= minimum_area_mm2 for _members, total_area in patches]
    queue: deque = deque(index for index, ok in enumerate(admitted) if ok)
    while queue:
        current_patch = queue.popleft()
        for neighbour_patch in patch_neighbours[current_patch]:
            if not admitted[neighbour_patch]:
                admitted[neighbour_patch] = True
                queue.append(neighbour_patch)

    admitted_candidates = {
        member
        for patch_index, (members, _total_area) in enumerate(patches)
        if admitted[patch_index]
        for member in members
    }

    # Re-group the admitted candidates by plain geometric adjacency (ignoring
    # normals entirely) so the reported patch count/area reflect the final,
    # merged regions rather than the original angle-based fragments.
    visited_final: Set[int] = set()
    protected = set()
    protected_patch_count = 0
    protected_area = 0.0
    for start in admitted_candidates:
        if start in visited_final:
            continue
        visited_final.add(start)
        group = [start]
        stack = [start]
        while stack:
            current = stack.pop()
            for neighbour in adjacency[current]:
                if neighbour in admitted_candidates and neighbour not in visited_final:
                    visited_final.add(neighbour)
                    group.append(neighbour)
                    stack.append(neighbour)
        protected_patch_count += 1
        protected_area += sum(areas[member] for member in group)
        for member in group:
            protected.add(source_indices[member])
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
    cell_size_mm: float = 1.0,
) -> Tuple[Set[int], dict[int, float]]:
    """Find triangles belonging to a geometrically disconnected piece of the
    same object that physically touches/crosses another piece, regardless
    of paint colour, and never fuzz them.

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

    flagged = [False] * count
    for candidate in range(count):
        if flagged[candidate]:
            continue
        shell = shell_of[candidate]
        centroid = centroids[candidate]
        for other in neighbours(triangle_cells[candidate]):
            if other == candidate or flagged[other] or shell_of[other] == shell:
                continue
            if _point_to_triangle_distance_approx(centroid, triangle_points[other]) > max_gap_mm:
                continue
            flagged[candidate] = True
            flagged[other] = True
            break

    protected = {source_indices[index] for index in range(count) if flagged[index]}
    area_by_index = {source_indices[index]: areas[index] for index in range(count) if flagged[index]}
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
    protect_overlap: bool = True,
    protect_nonmanifold: bool = True,
    report: Optional[ConversionReport] = None,
) -> Tuple[bytes, bool]:
    """Convert colour attributes in one 3MF model XML member."""
    report = report or ConversionReport()
    changed = False

    def convert_triangle_tags(
        xml: bytes,
        planar_protected: Set[int],
        overlap_protected: dict[int, float],
        nonmanifold_protected: dict[int, float],
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

            if triangle_index in overlap_protected:
                report.overlap_triangles_protected += 1
                report.overlap_area_protected_mm2 += overlap_protected[triangle_index]
                return tag

            if triangle_index in nonmanifold_protected:
                report.nonmanifold_triangles_protected += 1
                report.nonmanifold_area_protected_mm2 += nonmanifold_protected[triangle_index]
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

    if not protect_planar and not protect_overlap and not protect_nonmanifold:
        return convert_triangle_tags(data, set(), {}, {}), changed

    found_object = False

    def replace_object(match: re.Match) -> bytes:
        nonlocal found_object
        found_object = True
        object_xml = match.group(0)
        planar_protected: Set[int] = set()
        if protect_planar:
            planar_protected, patches, area = _find_planar_protection(
                object_xml,
                slots,
                angle_degrees=planar_angle_degrees,
                minimum_area_mm2=planar_minimum_area_mm2,
            )
            report.planar_patches_protected += patches
            report.planar_area_protected_mm2 += area
        overlap_protected: dict[int, float] = {}
        if protect_overlap:
            _overlap_indices, overlap_protected = _find_overlap_protection(object_xml)
        nonmanifold_protected: dict[int, float] = {}
        if protect_nonmanifold:
            _nonmanifold_indices, nonmanifold_protected = _find_nonmanifold_protection(object_xml)
        return convert_triangle_tags(
            object_xml, planar_protected, overlap_protected, nonmanifold_protected
        )

    converted = OBJECT_RE.sub(replace_object, data)
    if not found_object:
        converted = convert_triangle_tags(data, set(), {}, {})
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
    protect_overlap: bool = True,
    protect_nonmanifold: bool = True,
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
                            protect_overlap=protect_overlap,
                            protect_nonmanifold=protect_nonmanifold,
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
