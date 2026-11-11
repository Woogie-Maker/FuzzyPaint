//! Port of the detour-planning core nested inside
//! `gcode_layer_interval_detour.build_interval_detours`: `shift_point`,
//! `point_has_opposite`, and `plan_segment`. Step 4 of the port -- the
//! highest-value, highest-risk piece: it decides where the actual clipped
//! inset detour goes. The "both"/"upper"/"lower" mode orchestration, plan
//! merging, and emission to G-code (`finalize_layer`, `merge_segment_plans`,
//! `emit_plans`, `_emit_plan`) are still pure Python, deferred to step 5.
//!
//! These three functions are closures in the Python source (they capture
//! `inset_mm`, `boundary_resolution_mm`, etc. from `build_interval_detours`'s
//! own parameters), so unlike step 3's targets they can't be imported
//! directly for differential testing -- see the module doc comment in
//! `tests/test_rust_parity_planner.py` for how that's handled.

use crate::grid::{aligned, nearby_point, polyline_length, sample_points, GridKey};
use crate::segments::Segment;
use std::collections::HashMap;

/// The subset of `build_interval_detours`'s keyword parameters that
/// `shift_point`/`point_has_opposite`/`plan_segment` actually close over.
#[derive(Clone, Copy)]
pub struct PlanParams {
    pub inset_mm: f64,
    pub boundary_resolution_mm: f64,
    pub transition_distance_mm: f64,
    pub endpoint_coverage_mm: f64,
    pub inner_search_mm: f64,
    pub minimum_span_mm: f64,
    pub cell_mm: f64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Span {
    pub original: Vec<(f64, f64)>,
    pub shifted: Vec<(f64, f64)>,
    pub continue_in: bool,
    pub continue_out: bool,
}

#[derive(Debug, Clone, PartialEq)]
pub struct SegmentPlan {
    pub original: Vec<(f64, f64)>,
    pub spans: Vec<Span>,
}

fn distance(a: (f64, f64), b: (f64, f64)) -> f64 {
    (b.0 - a.0).hypot(b.1 - a.1)
}

/// Port of the `shift_point` closure.
pub fn shift_point(
    point: (f64, f64),
    segment: &Segment,
    inner_grid: &HashMap<GridKey, Vec<u32>>,
    inner_segments: &[Segment],
    params: &PlanParams,
) -> Option<(f64, f64)> {
    let (px, py) = point;
    let mut same_best: Option<(f64, f64)> = None;
    let mut any_best: Option<(f64, f64)> = None;
    let mut same_distance2 = params.inner_search_mm * params.inner_search_mm;
    let mut any_distance2 = params.inner_search_mm * params.inner_search_mm;

    for index in nearby_point(inner_grid, inner_segments, segment.object_id, px, py, params.inner_search_mm, params.cell_mm) {
        let candidate = &inner_segments[index];
        if !aligned(segment, candidate, 0.35) {
            continue;
        }
        let (qx, qy) = if candidate.length2 < 1e-12 {
            (candidate.x1, candidate.y1)
        } else {
            let projection = (0.0f64).max((1.0f64).min(
                ((px - candidate.x1) * candidate.dx + (py - candidate.y1) * candidate.dy) / candidate.length2,
            ));
            (candidate.x1 + projection * candidate.dx, candidate.y1 + projection * candidate.dy)
        };
        let distance2 = (px - qx).powi(2) + (py - qy).powi(2);
        if 0.0025 < distance2 && distance2 < any_distance2 {
            any_best = Some((qx, qy));
            any_distance2 = distance2;
        }
        if candidate.filament == segment.filament && 0.0025 < distance2 && distance2 < same_distance2 {
            same_best = Some((qx, qy));
            same_distance2 = distance2;
        }
    }

    let best = same_best.or(any_best)?;
    let best_distance = if same_best.is_some() { same_distance2.sqrt() } else { any_distance2.sqrt() };
    let amount = params.inset_mm.min(best_distance * 0.72);
    let (qx, qy) = best;
    Some((px + (qx - px) * amount / best_distance, py + (qy - py) * amount / best_distance))
}

/// Port of the `point_has_opposite` closure.
pub fn point_has_opposite(
    point: (f64, f64),
    segment: &Segment,
    reference_grid: &HashMap<GridKey, Vec<u32>>,
    reference_segments: &[Segment],
    params: &PlanParams,
) -> bool {
    let (px, py) = point;
    for index in nearby_point(
        reference_grid,
        reference_segments,
        segment.object_id,
        px,
        py,
        params.transition_distance_mm,
        params.cell_mm,
    ) {
        let candidate = &reference_segments[index];
        if candidate.length2 < 1e-12 {
            continue;
        }
        let projection =
            ((px - candidate.x1) * candidate.dx + (py - candidate.y1) * candidate.dy) / candidate.length2;
        let endpoint_margin =
            (params.endpoint_coverage_mm + params.boundary_resolution_mm * 0.5) / candidate.length.max(1e-9);
        if candidate.filament != segment.filament
            && aligned(segment, candidate, 0.45)
            && -endpoint_margin <= projection
            && projection <= 1.0 + endpoint_margin
        {
            let clipped = (0.0f64).max((1.0f64).min(projection));
            let qx = candidate.x1 + clipped * candidate.dx;
            let qy = candidate.y1 + clipped * candidate.dy;
            if (px - qx).powi(2) + (py - qy).powi(2) <= params.transition_distance_mm.powi(2) {
                return true;
            }
        }
    }
    false
}

/// Port of the `plan_segment` closure. Mutates `segment`'s
/// `interval_samples`/`interval_cumulative`/`interval_shifted` caches exactly
/// as the Python version does, including the implicit invariant (see the
/// module doc comment in the Python source) that the same `inner_grid` is
/// reused across any repeated call for a given segment in "both" mode --
/// this is trusted, not re-verified, matching the original.
pub fn plan_segment(
    segment: &mut Segment,
    inner_grid: &HashMap<GridKey, Vec<u32>>,
    inner_segments: &[Segment],
    reference_grid: &HashMap<GridKey, Vec<u32>>,
    reference_segments: &[Segment],
    params: &PlanParams,
) -> SegmentPlan {
    if segment.interval_samples.is_none() {
        segment.interval_samples = Some(sample_points(segment, params.boundary_resolution_mm));
    }
    let original = segment.interval_samples.clone().unwrap();

    let selected: Vec<bool> = original
        .windows(2)
        .map(|pair| {
            let midpoint = ((pair[0].0 + pair[1].0) * 0.5, (pair[0].1 + pair[1].1) * 0.5);
            point_has_opposite(midpoint, segment, reference_grid, reference_segments, params)
        })
        .collect();

    if segment.interval_cumulative.is_none() {
        let mut cumulative = vec![0.0f64];
        for pair in original.windows(2) {
            let last = *cumulative.last().unwrap();
            cumulative.push(last + distance(pair[0], pair[1]));
        }
        segment.interval_cumulative = Some(cumulative);
    }
    let cumulative = segment.interval_cumulative.clone().unwrap();

    let mut factors = vec![0.0f64; original.len()];
    let mut index = 0usize;
    while index < selected.len() {
        if !selected[index] {
            index += 1;
            continue;
        }
        let mut end = index + 1;
        while end < selected.len() && selected[end] {
            end += 1;
        }
        let span_points = &original[index..=end];
        if polyline_length(span_points) >= params.minimum_span_mm {
            let core_start = cumulative[index];
            let core_end = cumulative[end];
            for (point_index, &dist) in cumulative.iter().enumerate() {
                let factor = if dist < core_start {
                    1.0 - (core_start - dist) / params.transition_distance_mm.max(1e-9)
                } else if dist > core_end {
                    1.0 - (dist - core_end) / params.transition_distance_mm.max(1e-9)
                } else {
                    1.0
                };
                let clamped = (0.0f64).max((1.0f64).min(factor));
                factors[point_index] = factors[point_index].max(clamped);
            }
        }
        index = end;
    }

    let active: Vec<bool> = factors.iter().map(|&f| f > 1e-9).collect();
    let mut plan = SegmentPlan { original: original.clone(), spans: Vec::new() };
    let mut index = 0usize;
    while index < active.len() {
        if !active[index] {
            index += 1;
            continue;
        }
        let mut end = index + 1;
        while end < active.len() && active[end] {
            end += 1;
        }
        let first = index.saturating_sub(1);
        let last = (original.len() - 1).min(end);
        let original_points: Vec<(f64, f64)> = original[first..=last].to_vec();
        let mut shifted_points: Vec<(f64, f64)> = Vec::with_capacity(last - first + 1);
        let mut valid = true;
        for point_index in first..=last {
            let point = original[point_index];
            let factor = factors[point_index];
            if factor <= 1e-9 {
                shifted_points.push(point);
                continue;
            }
            if !segment.interval_shifted.contains_key(&point_index) {
                let shifted = shift_point(point, segment, inner_grid, inner_segments, params);
                segment.interval_shifted.insert(point_index, shifted);
            }
            match segment.interval_shifted.get(&point_index).copied().flatten() {
                Some(fully_shifted) => {
                    shifted_points.push((
                        point.0 + (fully_shifted.0 - point.0) * factor,
                        point.1 + (fully_shifted.1 - point.1) * factor,
                    ));
                }
                None => {
                    valid = false;
                    break;
                }
            }
        }
        if valid {
            plan.spans.push(Span { original: original_points, shifted: shifted_points, continue_in: false, continue_out: false });
        }
        index = end;
    }
    plan
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::grid::build_grid;

    fn seg(x1: f64, y1: f64, x2: f64, y2: f64, filament: i64, object_id: i64, line_number: usize) -> Segment {
        Segment::new(x1, y1, x2, y2, filament, object_id, line_number, b"G1".to_vec(), 0.1, 0.0, 0.0, None)
    }

    fn default_params() -> PlanParams {
        PlanParams {
            inset_mm: 0.24,
            boundary_resolution_mm: 0.10,
            transition_distance_mm: 0.55,
            endpoint_coverage_mm: 0.21,
            inner_search_mm: 1.2,
            minimum_span_mm: 0.10,
            cell_mm: 0.55,
        }
    }

    #[test]
    fn plans_a_detour_where_an_opposite_colour_is_nearby() {
        // Subject segment runs along y=0 from x=0..5, with an opposite-colour
        // wall running parallel directly below it for the middle portion, and
        // an inner wall to shift toward just above it.
        let mut subject = seg(0.0, 0.0, 5.0, 0.0, 0, 1, 10);
        let inner_segments = vec![seg(0.0, 0.3, 5.0, 0.3, 0, 1, 11)];
        let reference_segments = vec![seg(1.0, -0.2, 4.0, -0.2, 1, 1, 20)];
        let params = default_params();
        let inner_grid = build_grid(&inner_segments, params.cell_mm);
        let reference_grid = build_grid(&reference_segments, params.cell_mm);

        let plan = plan_segment(&mut subject, &inner_grid, &inner_segments, &reference_grid, &reference_segments, &params);

        assert!(!plan.spans.is_empty(), "expected at least one detour span");
        let span = &plan.spans[0];
        assert_eq!(span.original.len(), span.shifted.len());
        // The shifted points should have moved toward y>0 (toward the inner wall).
        for (&(_, oy), &(_, sy)) in span.original.iter().zip(span.shifted.iter()) {
            assert!(sy >= oy - 1e-9, "shifted point should not move away from the inner wall");
        }
        // Caches should now be populated.
        assert!(subject.interval_samples.is_some());
        assert!(subject.interval_cumulative.is_some());
    }

    #[test]
    fn no_opposite_colour_produces_no_spans() {
        let mut subject = seg(0.0, 0.0, 5.0, 0.0, 0, 1, 10);
        let inner_segments = vec![seg(0.0, 0.3, 5.0, 0.3, 0, 1, 11)];
        let reference_segments: Vec<Segment> = vec![];
        let params = default_params();
        let inner_grid = build_grid(&inner_segments, params.cell_mm);
        let reference_grid = build_grid(&reference_segments, params.cell_mm);

        let plan = plan_segment(&mut subject, &inner_grid, &inner_segments, &reference_grid, &reference_segments, &params);
        assert!(plan.spans.is_empty());
    }

    #[test]
    fn repeated_calls_reuse_the_cache() {
        let mut subject = seg(0.0, 0.0, 5.0, 0.0, 0, 1, 10);
        let inner_segments = vec![seg(0.0, 0.3, 5.0, 0.3, 0, 1, 11)];
        let reference_segments = vec![seg(1.0, -0.2, 4.0, -0.2, 1, 1, 20)];
        let params = default_params();
        let inner_grid = build_grid(&inner_segments, params.cell_mm);
        let reference_grid = build_grid(&reference_segments, params.cell_mm);

        let first = plan_segment(&mut subject, &inner_grid, &inner_segments, &reference_grid, &reference_segments, &params);
        let second = plan_segment(&mut subject, &inner_grid, &inner_segments, &reference_grid, &reference_segments, &params);
        assert_eq!(first, second);
    }
}
