//! Port of the spatial grid and neighbor search in `gcode_layer_detour.py`:
//! `_sample_points`, `_arc_points`, `_polyline_length`, `_closest_point`,
//! `_aligned`, `_grid`, `_nearby_point`. Step 3 of the port -- the detour
//! planning that consumes these (step 4) is still pure Python.

use std::collections::HashMap;

use crate::segments::Segment;

/// Mirrors `_distance` (`gcode_layer_interval_detour.py`): point-to-point
/// Euclidean distance. Shared by `planner.rs` and `emit.rs` too.
pub fn distance(a: (f64, f64), b: (f64, f64)) -> f64 {
    (b.0 - a.0).hypot(b.1 - a.1)
}

/// Mirrors `_same_point`.
pub fn same_point(a: (f64, f64), b: (f64, f64), tolerance: f64) -> bool {
    distance(a, b) <= tolerance
}

/// Port of `_arc_points`.
pub fn arc_points(segment: &Segment, maximum_step: f64) -> Vec<(f64, f64)> {
    let cx = segment.x1 + segment.i;
    let cy = segment.y1 + segment.j;
    let radius = (segment.x1 - cx).hypot(segment.y1 - cy);
    if radius < 1e-6 {
        return vec![(segment.x1, segment.y1), (segment.x2, segment.y2)];
    }
    let start = (segment.y1 - cy).atan2(segment.x1 - cx);
    let mut end = (segment.y2 - cy).atan2(segment.x2 - cx);
    if segment.command == b"G2" {
        while end >= start {
            end -= 2.0 * std::f64::consts::PI;
        }
    } else {
        while end <= start {
            end += 2.0 * std::f64::consts::PI;
        }
    }
    let sweep = end - start;
    let count = (1usize).max(((sweep.abs() * radius / maximum_step).ceil()) as usize);
    let mut points: Vec<(f64, f64)> = (0..=count)
        .map(|index| {
            let angle = start + sweep * (index as f64) / (count as f64);
            (cx + radius * angle.cos(), cy + radius * angle.sin())
        })
        .collect();
    let last = points.len() - 1;
    points[0] = (segment.x1, segment.y1);
    points[last] = (segment.x2, segment.y2);
    points
}

/// Port of `_sample_points`.
pub fn sample_points(segment: &Segment, spacing: f64) -> Vec<(f64, f64)> {
    if segment.command == b"G2" || segment.command == b"G3" {
        return arc_points(segment, spacing);
    }
    let count = (1usize).max(((segment.length / spacing).ceil()) as usize);
    // Deliberately written as `(x2 - x1) * index / count`, matching Python's
    // left-to-right `*`/`/` evaluation (`(x2 - x1) * index / count`, not
    // `(x2 - x1) * (index / count)`) -- the two associate differently in
    // floating point and must match bit-for-bit.
    (0..=count)
        .map(|index| {
            let index = index as f64;
            let count = count as f64;
            (
                segment.x1 + (segment.x2 - segment.x1) * index / count,
                segment.y1 + (segment.y2 - segment.y1) * index / count,
            )
        })
        .collect()
}

/// Port of `_polyline_length`.
pub fn polyline_length(points: &[(f64, f64)]) -> f64 {
    points.windows(2).map(|pair| (pair[1].0 - pair[0].0).hypot(pair[1].1 - pair[0].1)).sum()
}

/// Port of `_closest_point`.
pub fn closest_point(px: f64, py: f64, segment: &Segment) -> (f64, f64, f64) {
    if segment.length2 < 1e-12 {
        return (segment.x1, segment.y1, (px - segment.x1).hypot(py - segment.y1));
    }
    let t = (0.0f64)
        .max((1.0f64).min(((px - segment.x1) * segment.dx + (py - segment.y1) * segment.dy) / segment.length2));
    let qx = segment.x1 + t * segment.dx;
    let qy = segment.y1 + t * segment.dy;
    (qx, qy, (px - qx).hypot(py - qy))
}

/// Port of `_aligned`.
pub fn aligned(a: &Segment, b: &Segment, minimum: f64) -> bool {
    a.length > 1e-9 && b.length > 1e-9 && (a.unit_x * b.unit_x + a.unit_y * b.unit_y).abs() >= minimum
}

/// Grid cell key: `(object_id, cell_x, cell_y)`.
pub type GridKey = (i64, i64, i64);

/// Port of `_grid`: indices into `segments`, grouped by occupied cell. Stores
/// indices rather than cloning `Segment` data (the Python version stores
/// object references, which this mirrors without Rust's ownership getting in
/// the way).
pub fn build_grid(segments: &[Segment], cell: f64) -> HashMap<GridKey, Vec<u32>> {
    let mut result: HashMap<GridKey, Vec<u32>> = HashMap::new();
    for (index, segment) in segments.iter().enumerate() {
        let mut occupied: std::collections::HashSet<(i64, i64)> = std::collections::HashSet::new();
        for (x, y) in sample_points(segment, cell * 0.45) {
            occupied.insert(((x / cell).floor() as i64, (y / cell).floor() as i64));
        }
        for (gx, gy) in occupied {
            result.entry((segment.object_id, gx, gy)).or_default().push(index as u32);
        }
    }
    result
}

/// Port of `_nearby_point`: indices into `segments`, in the same order the
/// Python generator would yield them (outer `gx` loop, inner `gy` loop, each
/// cell's list in insertion order), deduplicated by `line_number` on first
/// occurrence.
pub fn nearby_point(
    grid: &HashMap<GridKey, Vec<u32>>,
    segments: &[Segment],
    object_id: i64,
    x: f64,
    y: f64,
    radius_mm: f64,
    cell_mm: f64,
) -> Vec<usize> {
    let cx = (x / cell_mm).floor() as i64;
    let cy = (y / cell_mm).floor() as i64;
    let radius = (radius_mm / cell_mm).ceil() as i64 + 1;
    let mut seen: std::collections::HashSet<usize> = std::collections::HashSet::new();
    let mut result = Vec::new();
    for gx in (cx - radius)..=(cx + radius) {
        for gy in (cy - radius)..=(cy + radius) {
            if let Some(indices) = grid.get(&(object_id, gx, gy)) {
                for &index in indices {
                    let index = index as usize;
                    let line_number = segments[index].line_number;
                    if seen.insert(line_number) {
                        result.push(index);
                    }
                }
            }
        }
    }
    result
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(x1: f64, y1: f64, x2: f64, y2: f64, object_id: i64, line_number: usize) -> Segment {
        Segment::new(x1, y1, x2, y2, 0, object_id, line_number, b"G1".to_vec(), 0.1, 0.0, 0.0, None)
    }

    #[test]
    fn polyline_length_sums_segment_distances() {
        assert_eq!(polyline_length(&[(0.0, 0.0), (3.0, 0.0), (3.0, 4.0)]), 7.0);
    }

    #[test]
    fn samples_straight_line_points() {
        let segment = seg(0.0, 0.0, 1.0, 0.0, 1, 1);
        let points = sample_points(&segment, 0.5);
        assert_eq!(points, vec![(0.0, 0.0), (0.5, 0.0), (1.0, 0.0)]);
    }

    #[test]
    fn closest_point_clamps_to_segment_ends() {
        let segment = seg(0.0, 0.0, 1.0, 0.0, 1, 1);
        assert_eq!(closest_point(-1.0, 0.0, &segment), (0.0, 0.0, 1.0));
        assert_eq!(closest_point(2.0, 0.0, &segment), (1.0, 0.0, 1.0));
        let (qx, qy, distance) = closest_point(0.5, 1.0, &segment);
        assert_eq!((qx, qy), (0.5, 0.0));
        assert_eq!(distance, 1.0);
    }

    #[test]
    fn aligned_requires_minimum_unit_dot_product() {
        let a = seg(0.0, 0.0, 1.0, 0.0, 1, 1);
        let parallel = seg(0.0, 5.0, 1.0, 5.0, 1, 2);
        let perpendicular = seg(0.0, 0.0, 0.0, 1.0, 1, 3);
        assert!(aligned(&a, &parallel, 0.35));
        assert!(!aligned(&a, &perpendicular, 0.35));
    }

    #[test]
    fn grid_and_nearby_point_find_segments_within_radius() {
        let segments = vec![
            seg(0.0, 0.0, 1.0, 0.0, 1, 1),
            seg(0.0, 10.0, 1.0, 10.0, 1, 2),
            seg(0.0, 0.0, 1.0, 0.0, 2, 3), // different object_id -> never matches
        ];
        let grid = build_grid(&segments, 0.55);
        let found = nearby_point(&grid, &segments, 1, 0.5, 0.0, 1.2, 0.55);
        let line_numbers: Vec<usize> = found.iter().map(|&i| segments[i].line_number).collect();
        assert_eq!(line_numbers, vec![1]);
    }
}
