use quick_xml::events::{BytesStart, Event};
use quick_xml::reader::Reader;
use std::collections::{HashMap, HashSet, VecDeque};

type Vec3 = [f64; 3];
type Cell = (i64, i64, i64);

#[derive(Clone)]
struct Triangle {
    source_index: usize,
    vertices: [usize; 3],
    points: [Vec3; 3],
    area: f64,
    normal: Vec3,
    minimum: Vec3,
    maximum: Vec3,
    selected: bool,
}

struct Geometry {
    vertices: Vec<Vec3>,
    triangles: Vec<Triangle>,
}

#[derive(Clone)]
struct PlanarPatch {
    members: Vec<usize>,
    total_area: f64,
    sum_normal: Vec3,
}

#[derive(Clone, Copy)]
pub struct PlanarOptions {
    pub angle_degrees: f64,
    pub minimum_area_mm2: f64,
    pub compact_strip_minimum_area_mm2: Option<f64>,
    pub compact_strip_minimum_aspect_ratio: f64,
    pub compact_strip_minimum_fill_ratio: f64,
    pub compact_strip_maximum_length_mm: f64,
    pub compact_strip_maximum_normal_z: f64,
    pub protect_connected_coplanar: bool,
    pub connected_coplanar_angle_degrees: f64,
    pub connected_coplanar_plane_tolerance_mm: f64,
    pub connected_coplanar_minimum_short_span_mm: f64,
    pub connected_coplanar_minimum_long_span_mm: f64,
    pub connected_coplanar_satellite_gap_mm: f64,
}

pub struct MeshProtectionResult {
    pub planar: Vec<usize>,
    pub planar_patch_count: usize,
    pub planar_area: f64,
    pub contacts: Vec<(usize, f64)>,
    pub envelope: Vec<(usize, f64)>,
}

fn local_name(name: &[u8]) -> &[u8] {
    name.rsplit(|byte| *byte == b':').next().unwrap_or(name)
}

fn attribute_value(tag: &BytesStart<'_>, wanted: &[u8]) -> Result<Option<Vec<u8>>, String> {
    for attribute in tag.attributes() {
        let attribute = attribute.map_err(|error| format!("invalid XML attribute: {error}"))?;
        if local_name(attribute.key.as_ref()).eq_ignore_ascii_case(wanted) {
            return Ok(Some(attribute.value.as_ref().to_vec()));
        }
    }
    Ok(None)
}

fn numeric_attribute<T: std::str::FromStr>(tag: &BytesStart<'_>, name: &[u8]) -> Result<Option<T>, String> {
    let Some(raw) = attribute_value(tag, name)? else { return Ok(None) };
    let text = std::str::from_utf8(&raw).map_err(|_| format!("attribute {} is not UTF-8", String::from_utf8_lossy(name)))?;
    text.parse::<T>()
        .map(Some)
        .map_err(|_| format!("attribute {} is not numeric", String::from_utf8_lossy(name)))
}

fn hex_nibble(byte: u8) -> Option<u8> {
    match byte {
        b'0'..=b'9' => Some(byte - b'0'),
        b'a'..=b'f' => Some(byte - b'a' + 10),
        b'A'..=b'F' => Some(byte - b'A' + 10),
        _ => None,
    }
}

fn paint_selects(value: &[u8], slots: Option<&HashSet<i64>>) -> Result<bool, String> {
    if value.is_empty() {
        return Err("empty paint stream".to_string());
    }
    let mut nibbles = Vec::with_capacity(value.len());
    for byte in value.iter().rev() {
        nibbles.push(hex_nibble(*byte).ok_or_else(|| "invalid paint stream".to_string())?);
    }

    fn parse(nibbles: &[u8], position: &mut usize, slots: Option<&HashSet<i64>>) -> Result<bool, String> {
        let code = *nibbles.get(*position).ok_or_else(|| "truncated paint stream".to_string())?;
        *position += 1;
        let split_sides = code & 0b11;
        if split_sides != 0 {
            let mut selected = false;
            for _ in 0..=split_sides {
                selected |= parse(nibbles, position, slots)?;
            }
            return Ok(selected);
        }
        let marker = code >> 2;
        let state = if marker < 3 {
            marker as i64
        } else {
            let mut state = 3i64;
            loop {
                let extension = *nibbles.get(*position).ok_or_else(|| "truncated paint stream".to_string())?;
                *position += 1;
                if extension == 0xF {
                    state += 15;
                } else {
                    break state + extension as i64;
                }
            }
        };
        Ok(state > 0 && slots.map_or(true, |values| values.contains(&state)))
    }

    let mut position = 0usize;
    let selected = parse(&nibbles, &mut position, slots)?;
    if position != nibbles.len() {
        return Err("paint stream has trailing nibbles".to_string());
    }
    Ok(selected)
}

fn subtract(left: Vec3, right: Vec3) -> Vec3 {
    [left[0] - right[0], left[1] - right[1], left[2] - right[2]]
}

fn cross(left: Vec3, right: Vec3) -> Vec3 {
    [
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    ]
}

fn dot(left: Vec3, right: Vec3) -> f64 {
    left[0] * right[0] + left[1] * right[1] + left[2] * right[2]
}

fn parse_geometry(object_xml: &[u8], selected_slots: Option<&[i64]>) -> Result<Geometry, String> {
    let slots = selected_slots.map(|values| values.iter().copied().collect::<HashSet<_>>());
    let mut reader = Reader::from_reader(object_xml);
    let mut vertices = Vec::new();
    let mut triangles = Vec::new();
    let mut triangle_source_index = 0usize;

    loop {
        let event = reader.read_event().map_err(|error| format!("XML parse error: {error}"))?;
        let tag = match event {
            Event::Empty(ref tag) | Event::Start(ref tag) => tag,
            Event::Eof => break,
            _ => continue,
        };
        let qualified_name = tag.name();
        let name = local_name(qualified_name.as_ref());
        if name.eq_ignore_ascii_case(b"vertex") {
            let Some(x) = numeric_attribute::<f64>(tag, b"x")? else { continue };
            let Some(y) = numeric_attribute::<f64>(tag, b"y")? else { continue };
            let Some(z) = numeric_attribute::<f64>(tag, b"z")? else { continue };
            vertices.push([x, y, z]);
        } else if name.eq_ignore_ascii_case(b"triangle") {
            let source_index = triangle_source_index;
            triangle_source_index += 1;
            let Some(v1) = numeric_attribute::<usize>(tag, b"v1")? else { continue };
            let Some(v2) = numeric_attribute::<usize>(tag, b"v2")? else { continue };
            let Some(v3) = numeric_attribute::<usize>(tag, b"v3")? else { continue };
            if v1 >= vertices.len() || v2 >= vertices.len() || v3 >= vertices.len() {
                continue;
            }
            let points = [vertices[v1], vertices[v2], vertices[v3]];
            let normal_raw = cross(subtract(points[1], points[0]), subtract(points[2], points[0]));
            let length = dot(normal_raw, normal_raw).sqrt();
            if length <= 1e-12 {
                continue;
            }
            let normal = [normal_raw[0] / length, normal_raw[1] / length, normal_raw[2] / length];
            let mut minimum = [0.0; 3];
            let mut maximum = [0.0; 3];
            for axis in 0..3 {
                minimum[axis] = points.iter().map(|point| point[axis]).fold(f64::INFINITY, f64::min);
                maximum[axis] = points.iter().map(|point| point[axis]).fold(f64::NEG_INFINITY, f64::max);
            }
            let selected = match attribute_value(tag, b"paint_color")? {
                Some(value) => paint_selects(&value, slots.as_ref())?,
                None => false,
            };
            triangles.push(Triangle {
                source_index,
                vertices: [v1, v2, v3],
                points,
                area: length * 0.5,
                normal,
                minimum,
                maximum,
                selected,
            });
        }
    }
    Ok(Geometry { vertices, triangles })
}

fn cells_for(minimum: Vec3, maximum: Vec3, margin: f64, cell_size: f64) -> Vec<Cell> {
    let low = [
        ((minimum[0] - margin) / cell_size).floor() as i64,
        ((minimum[1] - margin) / cell_size).floor() as i64,
        ((minimum[2] - margin) / cell_size).floor() as i64,
    ];
    let high = [
        ((maximum[0] + margin) / cell_size).floor() as i64,
        ((maximum[1] + margin) / cell_size).floor() as i64,
        ((maximum[2] + margin) / cell_size).floor() as i64,
    ];
    let mut result = Vec::new();
    for x in low[0]..=high[0] {
        for y in low[1]..=high[1] {
            for z in low[2]..=high[2] {
                result.push((x, y, z));
            }
        }
    }
    result
}

fn polygon_area(points: &[[f64; 2]]) -> f64 {
    if points.len() < 3 {
        return 0.0;
    }
    let mut total = 0.0;
    for index in 0..points.len() {
        let next = (index + 1) % points.len();
        total += points[index][0] * points[next][1] - points[next][0] * points[index][1];
    }
    0.5 * total.abs()
}

fn clipped_overlap_area(subject: &[[f64; 2]; 3], original_clip: &[[f64; 2]; 3]) -> f64 {
    let mut clip = *original_clip;
    let orientation: f64 = (0..3)
        .map(|index| clip[index][0] * clip[(index + 1) % 3][1] - clip[(index + 1) % 3][0] * clip[index][1])
        .sum();
    if orientation < 0.0 {
        clip.reverse();
    }
    let mut output = subject.to_vec();
    for edge_index in 0..3 {
        let edge_start = clip[edge_index];
        let edge_end = clip[(edge_index + 1) % 3];
        let input = output;
        output = Vec::new();
        if input.is_empty() {
            break;
        }
        let side = |point: [f64; 2]| {
            (edge_end[0] - edge_start[0]) * (point[1] - edge_start[1])
                - (edge_end[1] - edge_start[1]) * (point[0] - edge_start[0])
        };
        let mut previous = *input.last().unwrap();
        let mut previous_side = side(previous);
        for current in input {
            let current_side = side(current);
            if current_side >= -1e-9 {
                if previous_side < -1e-9 {
                    let ratio = previous_side / (previous_side - current_side);
                    output.push([
                        previous[0] + ratio * (current[0] - previous[0]),
                        previous[1] + ratio * (current[1] - previous[1]),
                    ]);
                }
                output.push(current);
            } else if previous_side >= -1e-9 {
                let ratio = previous_side / (previous_side - current_side);
                output.push([
                    previous[0] + ratio * (current[0] - previous[0]),
                    previous[1] + ratio * (current[1] - previous[1]),
                ]);
            }
            previous = current;
            previous_side = current_side;
        }
    }
    polygon_area(&output)
}

fn flat_contacts(geometry: &Geometry) -> HashSet<usize> {
    let max_gap = 0.05;
    let cell_size = 1.0;
    let cosine_limit = 2.0f64.to_radians().cos();
    let mut grid: HashMap<Cell, Vec<usize>> = HashMap::new();
    let mut triangle_cells = Vec::with_capacity(geometry.triangles.len());
    for (index, triangle) in geometry.triangles.iter().enumerate() {
        let cells = cells_for(triangle.minimum, triangle.maximum, max_gap, cell_size);
        for cell in &cells {
            grid.entry(*cell).or_default().push(index);
        }
        triangle_cells.push(cells);
    }

    let mut flagged = HashSet::new();
    let mut checked = HashSet::new();
    for (candidate, triangle) in geometry.triangles.iter().enumerate() {
        if !triangle.selected {
            continue;
        }
        let mut nearby = HashSet::new();
        for cell in &triangle_cells[candidate] {
            if let Some(indices) = grid.get(cell) {
                nearby.extend(indices.iter().copied());
            }
        }
        for other in nearby {
            if other == candidate {
                continue;
            }
            let other_triangle = &geometry.triangles[other];
            if (0..3).any(|axis| {
                triangle.maximum[axis] + max_gap < other_triangle.minimum[axis]
                    || other_triangle.maximum[axis] + max_gap < triangle.minimum[axis]
            }) {
                continue;
            }
            let pair = if candidate < other { (candidate, other) } else { (other, candidate) };
            if !checked.insert(pair) {
                continue;
            }
            if dot(triangle.normal, other_triangle.normal).abs() < cosine_limit {
                continue;
            }
            if dot(subtract(triangle.points[0], other_triangle.points[0]), other_triangle.normal).abs() > max_gap {
                continue;
            }
            let mut drop_axis = 0usize;
            for axis in 1..3 {
                if triangle.normal[axis].abs() > triangle.normal[drop_axis].abs() {
                    drop_axis = axis;
                }
            }
            let keep: Vec<usize> = (0..3).filter(|axis| *axis != drop_axis).collect();
            let first = triangle.points.map(|point| [point[keep[0]], point[keep[1]]]);
            let second = other_triangle.points.map(|point| [point[keep[0]], point[keep[1]]]);
            let scale = triangle.normal[drop_axis].abs();
            if scale <= 1e-9 || clipped_overlap_area(&first, &second) / scale < 0.01 {
                continue;
            }
            flagged.insert(candidate);
            if other_triangle.selected {
                flagged.insert(other);
            }
        }
    }
    flagged
}

fn rounded_point(point: Vec3) -> [i64; 3] {
    [
        (point[0] * 10_000.0).round_ties_even() as i64,
        (point[1] * 10_000.0).round_ties_even() as i64,
        (point[2] * 10_000.0).round_ties_even() as i64,
    ]
}

fn edge_key(left: Vec3, right: Vec3) -> ([i64; 3], [i64; 3]) {
    let left = rounded_point(left);
    let right = rounded_point(right);
    if left <= right { (left, right) } else { (right, left) }
}

fn plane_basis(normal: Vec3) -> (Vec3, Vec3) {
    let mut reference_axis = 0usize;
    for axis in 1..3 {
        if normal[axis].abs() < normal[reference_axis].abs() {
            reference_axis = axis;
        }
    }
    let mut reference = [0.0; 3];
    reference[reference_axis] = 1.0;
    let mut first = cross(normal, reference);
    let length = dot(first, first).sqrt();
    for value in &mut first {
        *value /= length;
    }
    (first, cross(normal, first))
}

fn principal_spans(points: &[Vec3], normal: Vec3) -> [f64; 2] {
    if points.len() < 2 {
        return [0.0, 0.0];
    }
    let (first, second) = plane_basis(normal);
    let coordinates: Vec<[f64; 2]> = points
        .iter()
        .map(|point| [dot(*point, first), dot(*point, second)])
        .collect();
    let mean_first = coordinates.iter().map(|value| value[0]).sum::<f64>() / coordinates.len() as f64;
    let mean_second = coordinates.iter().map(|value| value[1]).sum::<f64>() / coordinates.len() as f64;
    let centered: Vec<[f64; 2]> = coordinates
        .iter()
        .map(|value| [value[0] - mean_first, value[1] - mean_second])
        .collect();
    let covariance_first = centered.iter().map(|value| value[0] * value[0]).sum::<f64>();
    let covariance_second = centered.iter().map(|value| value[1] * value[1]).sum::<f64>();
    let covariance_cross = centered.iter().map(|value| value[0] * value[1]).sum::<f64>();
    let angle = 0.5 * (2.0 * covariance_cross).atan2(covariance_first - covariance_second);
    let cosine = angle.cos();
    let sine = angle.sin();
    let mut first_min = f64::INFINITY;
    let mut first_max = f64::NEG_INFINITY;
    let mut second_min = f64::INFINITY;
    let mut second_max = f64::NEG_INFINITY;
    for value in centered {
        let aligned_first = value[0] * cosine + value[1] * sine;
        let aligned_second = -value[0] * sine + value[1] * cosine;
        first_min = first_min.min(aligned_first);
        first_max = first_max.max(aligned_first);
        second_min = second_min.min(aligned_second);
        second_max = second_max.max(aligned_second);
    }
    let mut spans = [first_max - first_min, second_max - second_min];
    spans.sort_by(f64::total_cmp);
    spans
}

fn patch_points(geometry: &Geometry, members: &[usize]) -> Vec<Vec3> {
    let mut vertices = HashSet::new();
    for member in members {
        vertices.extend(geometry.triangles[*member].vertices);
    }
    let mut vertices: Vec<_> = vertices.into_iter().collect();
    vertices.sort_unstable();
    vertices.into_iter().map(|index| geometry.vertices[index]).collect()
}

fn compact_vertical_strip(geometry: &Geometry, patch: &PlanarPatch, options: PlanarOptions) -> bool {
    let Some(minimum_area) = options.compact_strip_minimum_area_mm2 else { return false };
    if patch.total_area < minimum_area {
        return false;
    }
    let normal_length = dot(patch.sum_normal, patch.sum_normal).sqrt();
    if normal_length <= 1e-12 {
        return false;
    }
    let normal = patch.sum_normal.map(|value| value / normal_length);
    if normal[2].abs() > options.compact_strip_maximum_normal_z {
        return false;
    }
    let horizontal_length = normal[0].hypot(normal[1]);
    if horizontal_length <= 1e-12 {
        return false;
    }
    let horizontal = [-normal[1] / horizontal_length, normal[0] / horizontal_length, 0.0];
    let vertical = cross(normal, horizontal);
    let points = patch_points(geometry, &patch.members);
    let horizontal_values: Vec<_> = points.iter().map(|point| dot(*point, horizontal)).collect();
    let vertical_values: Vec<_> = points.iter().map(|point| dot(*point, vertical)).collect();
    let horizontal_span = horizontal_values.iter().copied().fold(f64::NEG_INFINITY, f64::max)
        - horizontal_values.iter().copied().fold(f64::INFINITY, f64::min);
    let vertical_span = vertical_values.iter().copied().fold(f64::NEG_INFINITY, f64::max)
        - vertical_values.iter().copied().fold(f64::INFINITY, f64::min);
    let shorter = horizontal_span.min(vertical_span);
    let longer = horizontal_span.max(vertical_span);
    shorter > 1e-9
        && longer <= options.compact_strip_maximum_length_mm
        && longer / shorter >= options.compact_strip_minimum_aspect_ratio
        && patch.total_area / (horizontal_span * vertical_span) >= options.compact_strip_minimum_fill_ratio
}

fn planar_protection(geometry: &Geometry, options: PlanarOptions) -> (HashSet<usize>, usize, f64) {
    let mut adjacency = vec![Vec::new(); geometry.triangles.len()];
    let mut edge_owner: HashMap<([i64; 3], [i64; 3]), usize> = HashMap::new();
    for (candidate, triangle) in geometry.triangles.iter().enumerate() {
        for (left, right) in [(0, 1), (1, 2), (2, 0)] {
            let edge = edge_key(geometry.vertices[triangle.vertices[left]], geometry.vertices[triangle.vertices[right]]);
            if let Some(neighbor) = edge_owner.get(&edge).copied() {
                adjacency[neighbor].push(candidate);
                adjacency[candidate].push(neighbor);
            } else {
                edge_owner.insert(edge, candidate);
            }
        }
    }

    let cosine_limit = options.angle_degrees.to_radians().cos();
    let mut visited = vec![false; geometry.triangles.len()];
    let mut patches = Vec::new();
    for start in 0..geometry.triangles.len() {
        if visited[start] {
            continue;
        }
        visited[start] = true;
        let triangle = &geometry.triangles[start];
        let mut members = vec![start];
        let mut total_area = triangle.area;
        let mut sum_normal = triangle.normal.map(|value| value * triangle.area);
        let mut queue = VecDeque::from([start]);
        while let Some(current) = queue.pop_front() {
            for neighbor in &adjacency[current] {
                if visited[*neighbor] {
                    continue;
                }
                let length = dot(sum_normal, sum_normal).sqrt();
                if length <= 1e-12 {
                    continue;
                }
                let reference = sum_normal.map(|value| value / length);
                if dot(reference, geometry.triangles[*neighbor].normal) < cosine_limit {
                    continue;
                }
                visited[*neighbor] = true;
                members.push(*neighbor);
                queue.push_back(*neighbor);
                let candidate = &geometry.triangles[*neighbor];
                total_area += candidate.area;
                for axis in 0..3 {
                    sum_normal[axis] += candidate.normal[axis] * candidate.area;
                }
            }
        }
        patches.push(PlanarPatch { members, total_area, sum_normal });
    }

    let mut protected = HashSet::new();
    let mut patch_count = 0usize;
    let mut protected_area = 0.0;
    for patch in &patches {
        if patch.total_area < options.minimum_area_mm2 && !compact_vertical_strip(geometry, patch, options) {
            continue;
        }
        let newly: Vec<_> = patch.members.iter().copied().filter(|member| geometry.triangles[*member].selected).collect();
        if newly.is_empty() {
            continue;
        }
        patch_count += 1;
        protected_area += newly.iter().map(|member| geometry.triangles[*member].area).sum::<f64>();
        protected.extend(newly);
    }

    if !options.protect_connected_coplanar {
        return (protected, patch_count, protected_area);
    }

    #[derive(Clone)]
    struct StrictComponent {
        members: Vec<usize>,
        points: Vec<Vec3>,
        normal: Vec3,
        point: Vec3,
        minimum: Vec3,
        maximum: Vec3,
        qualifies: bool,
    }
    let strict_cosine = options.connected_coplanar_angle_degrees.to_radians().cos();
    let mut strict_visited = vec![false; geometry.triangles.len()];
    let mut components = Vec::new();
    for start in 0..geometry.triangles.len() {
        if strict_visited[start] {
            continue;
        }
        strict_visited[start] = true;
        let seed = &geometry.triangles[start];
        let mut members = vec![start];
        let mut queue = VecDeque::from([start]);
        while let Some(current) = queue.pop_front() {
            for neighbor in &adjacency[current] {
                if strict_visited[*neighbor] {
                    continue;
                }
                let candidate = &geometry.triangles[*neighbor];
                if dot(seed.normal, candidate.normal).abs() < strict_cosine {
                    continue;
                }
                if candidate.vertices.iter().any(|vertex| {
                    dot(subtract(geometry.vertices[*vertex], seed.points[0]), seed.normal).abs()
                        > options.connected_coplanar_plane_tolerance_mm
                }) {
                    continue;
                }
                strict_visited[*neighbor] = true;
                members.push(*neighbor);
                queue.push_back(*neighbor);
            }
        }
        let points = patch_points(geometry, &members);
        let spans = principal_spans(&points, seed.normal);
        let minimum = std::array::from_fn(|axis| points.iter().map(|point| point[axis]).fold(f64::INFINITY, f64::min));
        let maximum = std::array::from_fn(|axis| points.iter().map(|point| point[axis]).fold(f64::NEG_INFINITY, f64::max));
        let qualifies = members.len() >= 2
            && spans[0] >= options.connected_coplanar_minimum_short_span_mm
            && spans[1] >= options.connected_coplanar_minimum_long_span_mm;
        components.push(StrictComponent {
            members,
            points,
            normal: seed.normal,
            point: seed.points[0],
            minimum,
            maximum,
            qualifies,
        });
    }

    let qualifying: Vec<_> = components.iter().filter(|component| component.qualifies).cloned().collect();
    let satellite_cell_size = (options.connected_coplanar_satellite_gap_mm * 4.0).max(1.0);
    let mut anchor_grid: HashMap<Cell, Vec<usize>> = HashMap::new();
    for (anchor_index, anchor) in qualifying.iter().enumerate() {
        for cell in cells_for(
            anchor.minimum,
            anchor.maximum,
            options.connected_coplanar_satellite_gap_mm,
            satellite_cell_size,
        ) {
            anchor_grid.entry(cell).or_default().push(anchor_index);
        }
    }
    for component in &components {
        let mut qualifies = component.qualifies;
        if !qualifies && options.connected_coplanar_satellite_gap_mm > 0.0 {
            let nearby: HashSet<_> = cells_for(
                component.minimum,
                component.maximum,
                0.0,
                satellite_cell_size,
            )
            .into_iter()
            .filter_map(|cell| anchor_grid.get(&cell))
            .flatten()
            .copied()
            .collect();
            'anchors: for anchor_index in nearby {
                let anchor = &qualifying[anchor_index];
                let gaps: Vec<_> = (0..3)
                    .map(|axis| (anchor.minimum[axis] - component.maximum[axis]).max(component.minimum[axis] - anchor.maximum[axis]).max(0.0))
                    .collect();
                if gaps.iter().map(|gap| gap * gap).sum::<f64>().sqrt() > options.connected_coplanar_satellite_gap_mm
                    || dot(component.normal, anchor.normal).abs() < strict_cosine
                {
                    continue;
                }
                if component.points.iter().any(|point| {
                    dot(subtract(*point, anchor.point), anchor.normal).abs()
                        > options.connected_coplanar_plane_tolerance_mm
                }) {
                    continue;
                }
                qualifies = true;
                break 'anchors;
            }
        }
        if !qualifies {
            continue;
        }
        let newly: Vec<_> = component
            .members
            .iter()
            .copied()
            .filter(|member| geometry.triangles[*member].selected && !protected.contains(member))
            .collect();
        if newly.is_empty() {
            continue;
        }
        patch_count += 1;
        protected_area += newly.iter().map(|member| geometry.triangles[*member].area).sum::<f64>();
        protected.extend(newly);
    }
    (protected, patch_count, protected_area)
}

fn segment_hits_triangle(origin: Vec3, direction: Vec3, target: &Triangle, distance: f64) -> bool {
    let edge_one = subtract(target.points[1], target.points[0]);
    let edge_two = subtract(target.points[2], target.points[0]);
    let h = cross(direction, edge_two);
    let determinant = dot(edge_one, h);
    if determinant.abs() <= 1e-10 {
        return false;
    }
    let inverse = 1.0 / determinant;
    let offset = subtract(origin, target.points[0]);
    let u = inverse * dot(offset, h);
    if !(-1e-9..=1.0 + 1e-9).contains(&u) {
        return false;
    }
    let q = cross(offset, edge_one);
    let v = inverse * dot(direction, q);
    if v < -1e-9 || u + v > 1.0 + 1e-9 {
        return false;
    }
    let travel = inverse * dot(edge_two, q);
    travel > 1e-5 && travel <= distance + 1e-9
}

fn shares_vertex(left: &[usize; 3], right: &[usize; 3]) -> bool {
    left.iter().any(|value| right.contains(value))
}

fn fuzz_envelope(
    geometry: &Geometry,
    contact_seeds: &HashSet<usize>,
    distance: f64,
    neighbor_rings: usize,
) -> HashSet<usize> {
    if distance <= 0.0 || contact_seeds.is_empty() {
        return HashSet::new();
    }

    let mut edge_owners: HashMap<([i64; 3], [i64; 3]), Vec<usize>> = HashMap::new();
    let mut adjacency = vec![HashSet::new(); geometry.triangles.len()];
    for (index, triangle) in geometry.triangles.iter().enumerate() {
        for (left, right) in [(0, 1), (1, 2), (2, 0)] {
            let edge = edge_key(geometry.vertices[triangle.vertices[left]], geometry.vertices[triangle.vertices[right]]);
            if let Some(owners) = edge_owners.get(&edge) {
                for owner in owners {
                    adjacency[index].insert(*owner);
                    adjacency[*owner].insert(index);
                }
            }
            edge_owners.entry(edge).or_default().push(index);
        }
    }
    let mut eligible = contact_seeds.clone();
    let mut frontier = contact_seeds.clone();
    for _ in 0..neighbor_rings {
        let mut next = HashSet::new();
        for current in frontier {
            for neighbor in &adjacency[current] {
                if !eligible.contains(neighbor) {
                    next.insert(*neighbor);
                }
            }
        }
        if next.is_empty() {
            break;
        }
        eligible.extend(next.iter().copied());
        frontier = next;
    }

    let cell_size = 2.0;
    let mut grid: HashMap<Cell, Vec<usize>> = HashMap::new();
    for (index, triangle) in geometry.triangles.iter().enumerate() {
        for cell in cells_for(triangle.minimum, triangle.maximum, 0.0, cell_size) {
            grid.entry(cell).or_default().push(index);
        }
    }
    let weights = [
        [1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0],
        [0.80, 0.10, 0.10], [0.10, 0.80, 0.10], [0.10, 0.10, 0.80],
        [0.45, 0.45, 0.10], [0.10, 0.45, 0.45], [0.45, 0.10, 0.45],
    ];
    let mut flagged = HashSet::new();
    for candidate in eligible {
        let triangle = &geometry.triangles[candidate];
        if !triangle.selected {
            continue;
        }
        let mut nearby = HashSet::new();
        for cell in cells_for(triangle.minimum, triangle.maximum, distance, cell_size) {
            if let Some(indices) = grid.get(&cell) {
                nearby.extend(indices.iter().copied());
            }
        }
        nearby.remove(&candidate);
        let swept_minimum: Vec3 = std::array::from_fn(|axis| triangle.minimum[axis] - triangle.normal[axis].abs() * distance);
        let swept_maximum: Vec3 = std::array::from_fn(|axis| triangle.maximum[axis] + triangle.normal[axis].abs() * distance);
        let samples: Vec<Vec3> = weights.iter().map(|weight| {
            std::array::from_fn(|axis| (0..3).map(|index| weight[index] * triangle.points[index][axis]).sum())
        }).collect();
        let reverse = [-triangle.normal[0], -triangle.normal[1], -triangle.normal[2]];
        let mut hit = false;
        for other in nearby {
            let target = &geometry.triangles[other];
            if shares_vertex(&triangle.vertices, &target.vertices)
                || (0..3).any(|axis| target.maximum[axis] < swept_minimum[axis] - 1e-9 || target.minimum[axis] > swept_maximum[axis] + 1e-9)
            {
                continue;
            }
            if samples.iter().any(|sample| {
                segment_hits_triangle(*sample, triangle.normal, target, distance)
                    || segment_hits_triangle(*sample, reverse, target, distance)
            }) {
                hit = true;
                break;
            }
        }
        if hit {
            flagged.insert(candidate);
        }
    }
    flagged
}

pub fn find_flat_contact_and_fuzz_envelope(
    object_xml: &[u8],
    slots: Option<&[i64]>,
    distance_mm: f64,
    contact_neighbor_rings: usize,
) -> Result<(Vec<(usize, f64)>, Vec<(usize, f64)>), String> {
    let geometry = parse_geometry(object_xml, slots)?;
    if geometry.triangles.is_empty() || !geometry.triangles.iter().any(|triangle| triangle.selected) {
        return Ok((Vec::new(), Vec::new()));
    }
    let contacts = flat_contacts(&geometry);
    let envelope = fuzz_envelope(&geometry, &contacts, distance_mm, contact_neighbor_rings);
    let mut contact_result: Vec<_> = contacts.into_iter().map(|index| {
        let triangle = &geometry.triangles[index];
        (triangle.source_index, triangle.area)
    }).collect();
    let mut envelope_result: Vec<_> = envelope.into_iter().map(|index| {
        let triangle = &geometry.triangles[index];
        (triangle.source_index, triangle.area)
    }).collect();
    contact_result.sort_unstable_by_key(|item| item.0);
    envelope_result.sort_unstable_by_key(|item| item.0);
    Ok((contact_result, envelope_result))
}

pub fn find_mesh_protection(
    object_xml: &[u8],
    slots: Option<&[i64]>,
    planar_options: PlanarOptions,
    distance_mm: f64,
    contact_neighbor_rings: usize,
) -> Result<MeshProtectionResult, String> {
    let geometry = parse_geometry(object_xml, slots)?;
    if geometry.triangles.is_empty() || !geometry.triangles.iter().any(|triangle| triangle.selected) {
        return Ok(MeshProtectionResult {
            planar: Vec::new(),
            planar_patch_count: 0,
            planar_area: 0.0,
            contacts: Vec::new(),
            envelope: Vec::new(),
        });
    }
    let (planar_indices, planar_patch_count, planar_area) = planar_protection(&geometry, planar_options);
    let contacts = flat_contacts(&geometry);
    let envelope = fuzz_envelope(&geometry, &contacts, distance_mm, contact_neighbor_rings);
    let mut planar: Vec<_> = planar_indices
        .into_iter()
        .map(|index| geometry.triangles[index].source_index)
        .collect();
    let mut contact_result: Vec<_> = contacts
        .into_iter()
        .map(|index| (geometry.triangles[index].source_index, geometry.triangles[index].area))
        .collect();
    let mut envelope_result: Vec<_> = envelope
        .into_iter()
        .map(|index| (geometry.triangles[index].source_index, geometry.triangles[index].area))
        .collect();
    planar.sort_unstable();
    contact_result.sort_unstable_by_key(|item| item.0);
    envelope_result.sort_unstable_by_key(|item| item.0);
    Ok(MeshProtectionResult {
        planar,
        planar_patch_count,
        planar_area,
        contacts: contact_result,
        envelope: envelope_result,
    })
}
