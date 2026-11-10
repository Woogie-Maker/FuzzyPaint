"""Experimental same-filament outline generator for FuzzyPaint.

This converts selected colour-painted surface patches to fuzzy paint and also
adds a thin parameter-modifier shell immediately below those patches.  Bambu
Studio treats the modifier as a separate slicing region, which forces a real
toolpath boundary without introducing a second filament.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
import uuid
import zipfile
from collections import Counter
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET

from fuzzypaint import (
    COLOR_ATTR_RE,
    OBJECT_RE,
    TRIANGLE_RE,
    VERTEX_RE,
    ConversionError,
    PaintNode,
    _find_planar_protection,
    _remap_extruder_metadata,
    _simple_attribute,
    _strip_all_colour_paint,
    convert_model_xml,
    decode_paint_stream,
)


Vec3 = tuple[float, float, float]
Tri = tuple[Vec3, Vec3, Vec3]


def _midpoint(a: Vec3, b: Vec3) -> Vec3:
    return tuple((a[i] + b[i]) * 0.5 for i in range(3))  # type: ignore[return-value]


def _split_triangle(tri: Tri, split_code: int) -> list[Tri]:
    count = split_code & 0b11
    side = split_code >> 2
    rotated = [tri[(side + i) % 3] for i in range(3)]
    a, b, c = rotated
    if count == 1:
        m = _midpoint(c, b)
        return [(a, b, m), (m, c, a)]
    if count == 2:
        ab, ac = _midpoint(b, a), _midpoint(a, c)
        return [(a, ab, ac), (ab, b, ac), (b, c, ac)]
    if count == 3:
        ab, bc, ca = _midpoint(b, a), _midpoint(c, b), _midpoint(a, c)
        return [(a, ab, ca), (ab, b, bc), (bc, c, ca), (ab, bc, ca)]
    raise ConversionError(f"Invalid paint split code: {split_code}")


def _selected_subtriangles(
    node: PaintNode, tri: Tri, selected_slots: set[int]
) -> Iterable[Tri]:
    if node.is_leaf:
        if int(node.state or 0) in selected_slots:
            yield tri
        return
    if node.split_code is None:
        return
    # The TriangleSelector serialization consumes children in reverse order.
    geometry = list(reversed(_split_triangle(tri, node.split_code)))
    for child, child_tri in zip(node.children, geometry):
        yield from _selected_subtriangles(child, child_tri, selected_slots)


def _normal(tri: Tri) -> Vec3:
    a, b, c = tri
    ab = tuple(b[i] - a[i] for i in range(3))
    ac = tuple(c[i] - a[i] for i in range(3))
    cross = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    length = math.sqrt(sum(v * v for v in cross))
    return (0.0, 0.0, 0.0) if length < 1e-12 else tuple(v / length for v in cross)  # type: ignore[return-value]


def _point_key(point: Vec3) -> tuple[int, int, int]:
    # Painted subdivision points are repeated midpoint calculations.  A small
    # quantisation joins their patch edges without altering emitted positions.
    return tuple(round(value * 10**7) for value in point)  # type: ignore[return-value]


def _modifier_shell(triangles: list[Tri], thickness: float = 0.8) -> tuple[list[Vec3], list[tuple[int, int, int]]]:
    points: list[Vec3] = []
    point_ids: dict[tuple[int, int, int], int] = {}
    faces: list[tuple[int, int, int]] = []
    normal_sums: list[list[float]] = []
    edge_counts: Counter[tuple[int, int]] = Counter()
    oriented_edges: dict[tuple[int, int], tuple[int, int]] = {}

    def vertex_id(point: Vec3) -> int:
        key = _point_key(point)
        found = point_ids.get(key)
        if found is not None:
            return found
        found = len(points)
        point_ids[key] = found
        points.append(point)
        normal_sums.append([0.0, 0.0, 0.0])
        return found

    for tri in triangles:
        ids = tuple(vertex_id(point) for point in tri)
        if len(set(ids)) != 3:
            continue
        n = _normal(tri)
        if n == (0.0, 0.0, 0.0):
            continue
        faces.append(ids)
        for idx in ids:
            for axis in range(3):
                normal_sums[idx][axis] += n[axis]
        for edge in ((ids[0], ids[1]), (ids[1], ids[2]), (ids[2], ids[0])):
            key = tuple(sorted(edge))
            edge_counts[key] += 1
            oriented_edges.setdefault(key, edge)

    normals: list[Vec3] = []
    for values in normal_sums:
        length = math.sqrt(sum(v * v for v in values))
        normals.append((0.0, 0.0, 1.0) if length < 1e-12 else tuple(v / length for v in values))

    # Let the shell protrude 0.02 mm beyond the source face to guarantee a
    # robust intersection.  Only the intersection with the model is sliced.
    outer = [tuple(p[i] + normals[j][i] * 0.02 for i in range(3)) for j, p in enumerate(points)]
    inner = [tuple(p[i] - normals[j][i] * thickness for i in range(3)) for j, p in enumerate(points)]
    count = len(points)
    shell_faces: list[tuple[int, int, int]] = []
    shell_faces.extend(faces)
    shell_faces.extend((c + count, b + count, a + count) for a, b, c in faces)
    for key, occurrences in edge_counts.items():
        if occurrences != 1:
            continue
        a, b = oriented_edges[key]
        shell_faces.extend(((a, b + count, b), (a, a + count, b + count)))
    return outer + inner, shell_faces


def _mesh_from_object(object_xml: bytes, selected_slots: set[int]) -> tuple[list[Vec3], list[tuple[int, int, int]]]:
    vertices: list[Vec3] = []
    for match in VERTEX_RE.finditer(object_xml):
        tag = match.group(0)
        values = [_simple_attribute(tag, key) for key in (b"x", b"y", b"z")]
        if any(value is None for value in values):
            raise ConversionError("A model vertex is missing coordinates")
        vertices.append(tuple(float(value) for value in values))  # type: ignore[arg-type]

    protected, _, _ = _find_planar_protection(
        object_xml, selected_slots, angle_degrees=2.0, minimum_area_mm2=20.0
    )
    selected: list[Tri] = []
    for triangle_index, match in enumerate(TRIANGLE_RE.finditer(object_xml)):
        if triangle_index in protected:
            continue
        tag = match.group(0)
        paint = COLOR_ATTR_RE.search(tag)
        if not paint:
            continue
        raw_ids = [_simple_attribute(tag, key) for key in (b"v1", b"v2", b"v3")]
        if any(value is None for value in raw_ids):
            continue
        ids = tuple(int(value) for value in raw_ids)  # type: ignore[arg-type]
        tri = tuple(vertices[index] for index in ids)
        selected.extend(
            _selected_subtriangles(
                decode_paint_stream(paint.group("value").decode("ascii")), tri, selected_slots
            )
        )
    return _modifier_shell(selected) if selected else ([], [])


def _object_xml(object_id: int, vertices: list[Vec3], faces: list[tuple[int, int, int]]) -> bytes:
    lines = [
        f'  <object id="{object_id}" p:UUID="{uuid.uuid4()}" type="model">',
        "   <mesh>",
        "    <vertices>",
    ]
    lines.extend(
        f'     <vertex x="{x:.9g}" y="{y:.9g}" z="{z:.9g}"/>' for x, y, z in vertices
    )
    lines.append("    </vertices>")
    lines.append("    <triangles>")
    lines.extend(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in faces)
    lines.extend(("    </triangles>", "   </mesh>", "  </object>"))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _id_from_object_tag(object_xml: bytes) -> int:
    opening = object_xml[: object_xml.find(b">") + 1]
    value = _simple_attribute(opening, b"id")
    if value is None:
        raise ConversionError("A painted 3MF object has no id")
    return int(value)


def _add_component(root_xml: bytes, original_id: int, new_id: int) -> bytes:
    pattern = re.compile(
        rb"<component\b(?=[^>]*\bobjectid\s*=\s*([\"'])" + str(original_id).encode() + rb"\1)[^>]*/>",
        re.IGNORECASE,
    )
    match = pattern.search(root_xml)
    if not match:
        raise ConversionError(f"Could not find the assembly component for object {original_id}")
    original = match.group(0)
    duplicate = re.sub(
        rb"(\bobjectid\s*=\s*)([\"']).*?\2",
        lambda m: m.group(1) + m.group(2) + str(new_id).encode() + m.group(2),
        original,
        count=1,
    )
    duplicate = re.sub(
        rb"(\bp:UUID\s*=\s*)([\"']).*?\2",
        lambda m: m.group(1) + m.group(2) + str(uuid.uuid4()).encode() + m.group(2),
        duplicate,
        count=1,
    )
    return root_xml[: match.end()] + b"\n    " + duplicate + root_xml[match.end() :]


def _add_modifier_part(config: bytes, original_id: int, new_id: int, face_count: int) -> bytes:
    root = ET.fromstring(config)
    target_parent = None
    source_part = None
    for obj in root.findall("object"):
        for part in obj.findall("part"):
            if int(part.get("id", "-1")) == original_id:
                target_parent, source_part = obj, part
                break
    if target_parent is None or source_part is None:
        raise ConversionError(f"Could not find model settings for painted part {original_id}")

    part = ET.Element(
        "part",
        {"id": str(new_id), "subtype": "modifier_part", "uuid": str(uuid.uuid4())},
    )
    ET.SubElement(part, "metadata", {"key": "name", "value": "FuzzyPaint moss outline"})
    matrix = next((m.get("value") for m in source_part.findall("metadata") if m.get("key") == "matrix"), None)
    if matrix:
        ET.SubElement(part, "metadata", {"key": "matrix", "value": matrix})
    ET.SubElement(part, "metadata", {"key": "fuzzy_skin", "value": "external"})
    ET.SubElement(
        part,
        "mesh_stat",
        {
            "face_count": str(face_count),
            "edges_fixed": "0",
            "degenerate_facets": "0",
            "facets_removed": "0",
            "facets_reversed": "0",
            "backwards_edges": "0",
        },
    )
    target_parent.append(part)
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def build_outlined_single_colour(
    source: os.PathLike | str, destination: os.PathLike | str, selected_slots: set[int]
) -> tuple[int, int]:
    source_path, destination_path = Path(source), Path(destination)
    with zipfile.ZipFile(source_path) as archive:
        infos = archive.infolist()
        payloads = {info.filename: archive.read(info) for info in infos}

    model_names = [name for name in payloads if name.lower().endswith(".model")]
    all_ids = []
    for name in model_names:
        all_ids.extend(int(value) for value in re.findall(rb"<object\b[^>]*\bid\s*=\s*[\"'](\d+)", payloads[name]))
    next_id = max(all_ids, default=0) + 1
    additions: list[tuple[int, int, int]] = []

    for name in model_names:
        if name.lower() == "3d/3dmodel.model":
            continue
        original_data = payloads[name]
        generated = []
        for match in OBJECT_RE.finditer(original_data):
            object_xml = match.group(0)
            if b"paint_color" not in object_xml:
                continue
            vertices, faces = _mesh_from_object(object_xml, selected_slots)
            if not faces:
                continue
            original_id, modifier_id = _id_from_object_tag(object_xml), next_id
            next_id += 1
            generated.append(_object_xml(modifier_id, vertices, faces))
            additions.append((original_id, modifier_id, len(faces)))
        if generated:
            close = original_data.rfind(b"</resources>")
            if close < 0:
                raise ConversionError(f"Malformed 3MF model member: {name}")
            original_data = original_data[:close] + b"".join(generated) + original_data[close:]
        converted, _ = convert_model_xml(
            original_data,
            slots=selected_slots,
            remove_colour=False,
            protect_planar=True,
        )
        payloads[name], _ = _strip_all_colour_paint(converted)

    root_name = next(name for name in payloads if name.lower() == "3d/3dmodel.model")
    root_xml = payloads[root_name]
    for original_id, modifier_id, _ in additions:
        root_xml = _add_component(root_xml, original_id, modifier_id)
    payloads[root_name] = root_xml

    config_name = next(name for name in payloads if name.lower() == "metadata/model_settings.config")
    config = payloads[config_name]
    for original_id, modifier_id, faces in additions:
        config = _add_modifier_part(config, original_id, modifier_id, faces)
    config, _ = _remap_extruder_metadata(config, 1)
    payloads[config_name] = config

    project_name = next(name for name in payloads if name.lower() == "metadata/project_settings.config")
    # A genuine one-filament plate makes Bambu disable the tower automatically.
    import json
    settings = json.loads(payloads[project_name].decode("utf-8-sig"))
    settings["enable_prime_tower"] = "0"
    payloads[project_name] = json.dumps(settings, ensure_ascii=False, indent=4).encode() + b"\n"

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=destination_path.stem, suffix=".tmp", dir=destination_path.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temporary, "w", allowZip64=True) as output:
            for info in infos:
                output.writestr(info, payloads[info.filename])
        os.replace(temporary, destination_path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return len(additions), sum(faces for _, _, faces in additions)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--slots", default="2")
    args = parser.parse_args()
    slots = {int(value) for value in args.slots.split(",")}
    count, faces = build_outlined_single_colour(args.input, args.output, slots)
    print(f"Added {count} outline modifier(s), {faces:,} faces")
