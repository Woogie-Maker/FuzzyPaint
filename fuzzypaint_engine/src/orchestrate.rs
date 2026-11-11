//! Port of `finalize_layer` and its helpers (`merge_segment_plans`,
//! `merge_plan_sets`, `emit_plans`, the same-layer-gap channel trim) from
//! `gcode_layer_interval_detour.build_interval_detours`. Step 5b of the
//! port -- the layer-to-layer orchestration that drives step 4's planner
//! and step 5a's emitter across a whole file, for all three `detour_side`
//! modes. "Both" is the GUI's actual default, not an edge case, so all
//! three modes are implemented here, not just "upper".

use std::collections::HashMap;

use crate::emit::{emit_plan, format_g};
use crate::grid::{aligned, build_grid, polyline_length, same_point, GridKey};
use crate::planner::{plan_segment, PlanParams, SegmentPlan, Span};
use crate::segments::{LayerSegments, Segment};

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum DetourSide {
    Upper,
    Lower,
    Both,
}

pub struct OrchestrateParams {
    pub plan: PlanParams,
    pub detour_side: DetourSide,
    pub target_layers: Option<std::collections::HashSet<i64>>,
    pub subject_filaments: Option<std::collections::HashSet<i64>>,
    pub same_layer_gap_mm: f64,
    pub same_layer_endpoint_tolerance_mm: f64,
}

/// A planned segment paired with the (cloned, post-plan) `Segment` metadata
/// `merge_segment_plans`/`emit_plans` need (line_number, object_id, filament,
/// endpoints, command, extrusion, feed). Mirrors Python's `SegmentPlan.segment`.
pub struct PlannedSegment {
    pub segment: Segment,
    pub plan: SegmentPlan,
}

fn eligible_indices(segments: &[Segment], subject_filaments: &Option<std::collections::HashSet<i64>>) -> Vec<usize> {
    match subject_filaments {
        None => (0..segments.len()).collect(),
        Some(set) => (0..segments.len()).filter(|&i| set.contains(&segments[i].filament)).collect(),
    }
}

fn point_key(point: (f64, f64)) -> (u64, u64) {
    (point.0.to_bits(), point.1.to_bits())
}

/// Port of `merge_segment_plans`.
pub fn merge_segment_plans(first: PlannedSegment, second: PlannedSegment) -> PlannedSegment {
    let same_shape = first.plan.original.len() == second.plan.original.len()
        && first
            .plan
            .original
            .iter()
            .zip(second.plan.original.iter())
            .all(|(&a, &b)| same_point(a, b, 1e-6));
    if !same_shape {
        let first_length: f64 = first.plan.spans.iter().map(|s| polyline_length(&s.original)).sum();
        let second_length: f64 = second.plan.spans.iter().map(|s| polyline_length(&s.original)).sum();
        return if first_length >= second_length { first } else { second };
    }

    let original = first.plan.original.clone();
    let mut point_indices: HashMap<(u64, u64), Vec<usize>> = HashMap::new();
    for (index, &point) in original.iter().enumerate() {
        point_indices.entry(point_key(point)).or_default().push(index);
    }
    let mut selected = vec![false; original.len().saturating_sub(1)];
    let mut shifted_points: HashMap<usize, (f64, f64)> = HashMap::new();
    for planned in [&first, &second] {
        for span in &planned.plan.spans {
            let starts = point_indices.get(&point_key(span.original[0]));
            let ends = point_indices.get(&point_key(*span.original.last().unwrap()));
            let (starts, ends) = match (starts, ends) {
                (Some(s), Some(e)) if !s.is_empty() && !e.is_empty() => (s, e),
                _ => continue,
            };
            let start = starts[0];
            let end = ends.iter().copied().find(|&index| index >= start).unwrap_or(*ends.last().unwrap());
            for index in start..end {
                if index < selected.len() {
                    selected[index] = true;
                }
            }
            for (offset, &point) in span.shifted.iter().enumerate() {
                shifted_points.insert(start + offset, point);
            }
        }
    }

    let mut merged_spans = Vec::new();
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
        let shifted: Option<Vec<(f64, f64)>> = (index..=end).map(|i| shifted_points.get(&i).copied()).collect();
        if let Some(shifted) = shifted {
            merged_spans.push(Span {
                original: original[index..=end].to_vec(),
                shifted,
                continue_in: false,
                continue_out: false,
            });
        }
        index = end;
    }

    PlannedSegment { segment: first.segment, plan: SegmentPlan { original, spans: merged_spans } }
}

/// Port of `merge_plan_sets`. (Insertion order doesn't matter: `emit_plans`
/// re-sorts by line_number immediately, same as the Python version relying
/// on that sort rather than dict order.)
pub fn merge_plan_sets(plan_sets: Vec<Vec<PlannedSegment>>) -> Vec<PlannedSegment> {
    let mut merged: HashMap<usize, PlannedSegment> = HashMap::new();
    for plans in plan_sets {
        for plan in plans {
            let line_number = plan.segment.line_number;
            match merged.remove(&line_number) {
                Some(existing) => {
                    merged.insert(line_number, merge_segment_plans(existing, plan));
                }
                None => {
                    merged.insert(line_number, plan);
                }
            }
        }
    }
    merged.into_values().collect()
}

/// Port of `emit_plans`.
pub fn emit_plans(mut plans: Vec<PlannedSegment>) -> HashMap<usize, Vec<u8>> {
    plans.retain(|p| !p.plan.spans.is_empty());
    plans.sort_by_key(|p| p.segment.line_number);

    for i in 0..plans.len().saturating_sub(1) {
        let (left, right) = plans.split_at_mut(i + 1);
        let previous = &mut left[i];
        let following = &mut right[0];
        let adjacent = previous.segment.object_id == following.segment.object_id
            && previous.segment.filament == following.segment.filament
            && (following.segment.line_number as i64 - previous.segment.line_number as i64) < 12
            && same_point(
                (previous.segment.x2, previous.segment.y2),
                (following.segment.x1, following.segment.y1),
                0.03,
            )
            && same_point(
                *previous.plan.spans.last().unwrap().original.last().unwrap(),
                (previous.segment.x2, previous.segment.y2),
                0.025,
            )
            && same_point(following.plan.spans[0].original[0], (following.segment.x1, following.segment.y1), 0.025);
        if adjacent {
            let prev_last_shifted = *previous.plan.spans.last().unwrap().shifted.last().unwrap();
            let next_first_shifted = following.plan.spans[0].shifted[0];
            let join = (
                (prev_last_shifted.0 + next_first_shifted.0) * 0.5,
                (prev_last_shifted.1 + next_first_shifted.1) * 0.5,
            );
            let previous_last_span = previous.plan.spans.last_mut().unwrap();
            let last_index = previous_last_span.shifted.len() - 1;
            previous_last_span.shifted[last_index] = join;
            previous_last_span.continue_out = true;
            let following_first_span = &mut following.plan.spans[0];
            following_first_span.shifted[0] = join;
            following_first_span.continue_in = true;
        }
    }

    let mut replacements = HashMap::new();
    for planned in plans {
        let line_number = planned.segment.line_number;
        replacements.insert(line_number, emit_plan(planned.plan, &planned.segment));
    }
    replacements
}

/// Port of the same-layer-gap channel trim (the tail of `finalize_layer`,
/// `detour_side == "upper"` only). `existing_replacements` is the running
/// global replacements map built up so far -- a line already covered by a
/// detour plan is skipped, matching `if line_number in replacements: continue`.
pub fn same_layer_channel_trims(
    outer: &[Segment],
    same_layer_gap_mm: f64,
    same_layer_endpoint_tolerance_mm: f64,
    existing_replacements: &HashMap<usize, Vec<u8>>,
) -> HashMap<usize, Vec<u8>> {
    let endpoint_cell = (same_layer_endpoint_tolerance_mm * 2.0).max(0.02);
    let mut endpoint_grid: HashMap<(i64, i64, i64), Vec<(usize, bool)>> = HashMap::new();
    let mut endpoints: Vec<(usize, bool, f64, f64)> = Vec::new();
    for (index, segment) in outer.iter().enumerate() {
        if segment.command != b"G1" {
            continue;
        }
        let length = (segment.x2 - segment.x1).hypot(segment.y2 - segment.y1);
        if length <= same_layer_gap_mm * 1.25 {
            continue;
        }
        for &(at_start, px, py) in &[(true, segment.x1, segment.y1), (false, segment.x2, segment.y2)] {
            endpoints.push((index, at_start, px, py));
            let gx = (px / endpoint_cell).floor() as i64;
            let gy = (py / endpoint_cell).floor() as i64;
            endpoint_grid.entry((segment.object_id, gx, gy)).or_default().push((index, at_start));
        }
    }

    let mut trims: HashMap<usize, (bool, bool)> = HashMap::new();
    for &(index, at_start, px, py) in &endpoints {
        let segment = &outer[index];
        let cx = (px / endpoint_cell).floor() as i64;
        let cy = (py / endpoint_cell).floor() as i64;
        let mut found = false;
        'cells: for gx in (cx - 1)..=(cx + 1) {
            for gy in (cy - 1)..=(cy + 1) {
                if let Some(candidates) = endpoint_grid.get(&(segment.object_id, gx, gy)) {
                    for &(other_index, other_start) in candidates {
                        let other = &outer[other_index];
                        if other.line_number == segment.line_number || other.filament == segment.filament {
                            continue;
                        }
                        if !aligned(segment, other, 0.75) {
                            continue;
                        }
                        let (qx, qy) = if other_start { (other.x1, other.y1) } else { (other.x2, other.y2) };
                        let junction_distance = (px - qx).hypot(py - qy);
                        if !(0.20 <= junction_distance && junction_distance <= same_layer_endpoint_tolerance_mm) {
                            continue;
                        }
                        let vx = (qx - px) / junction_distance;
                        let vy = (qy - py) / junction_distance;
                        let segment_length = (segment.x2 - segment.x1).hypot(segment.y2 - segment.y1);
                        let other_length = (other.x2 - other.x1).hypot(other.y2 - other.y1);
                        let (sx, sy) = if at_start {
                            ((segment.x1 - segment.x2) / segment_length, (segment.y1 - segment.y2) / segment_length)
                        } else {
                            ((segment.x2 - segment.x1) / segment_length, (segment.y2 - segment.y1) / segment_length)
                        };
                        let (ox, oy) = if other_start {
                            ((other.x1 - other.x2) / other_length, (other.y1 - other.y2) / other_length)
                        } else {
                            ((other.x2 - other.x1) / other_length, (other.y2 - other.y1) / other_length)
                        };
                        if sx * vx + sy * vy < 0.70 || ox * -vx + oy * -vy < 0.70 {
                            continue;
                        }
                        let entry = trims.entry(segment.line_number).or_insert((false, false));
                        if at_start {
                            entry.0 = true;
                        } else {
                            entry.1 = true;
                        }
                        found = true;
                        break;
                    }
                }
                if found {
                    break 'cells;
                }
            }
        }
    }

    let half_gap = same_layer_gap_mm * 0.5;
    let mut by_line: HashMap<usize, &Segment> = HashMap::new();
    for segment in outer {
        by_line.insert(segment.line_number, segment);
    }
    let mut result = HashMap::new();
    for (line_number, (trim_start, trim_end)) in trims {
        if existing_replacements.contains_key(&line_number) {
            continue;
        }
        let segment = by_line[&line_number];
        let length = (segment.x2 - segment.x1).hypot(segment.y2 - segment.y1);
        let start_amount = if trim_start { half_gap.min(length * 0.2) } else { 0.0 };
        let end_amount = if trim_end { half_gap.min(length * 0.2) } else { 0.0 };
        if start_amount + end_amount >= length * 0.8 {
            continue;
        }
        let ux = (segment.x2 - segment.x1) / length;
        let uy = (segment.y2 - segment.y1) / length;
        let start = (segment.x1 + ux * start_amount, segment.y1 + uy * start_amount);
        let end = (segment.x2 - ux * end_amount, segment.y2 - uy * end_amount);
        let mut lines: Vec<String> = Vec::new();
        if trim_start {
            lines.push(format!("G0 X{:.5} Y{:.5} ; FuzzyPaint same-layer channel start", start.0, start.1));
        }
        let extrusion = segment.extrusion * (length - start_amount - end_amount) / length;
        let feed = match segment.feed {
            Some(f) => format!(" F{}", format_g(f)),
            None => String::new(),
        };
        lines.push(format!("G1 X{:.5} Y{:.5} E{:.6}{} ; FuzzyPaint same-layer channel span", end.0, end.1, extrusion, feed));
        if trim_end {
            lines.push(format!("G0 X{:.5} Y{:.5} ; FuzzyPaint same-layer channel end", segment.x2, segment.y2));
        }
        let mut bytes = lines.join("\n").into_bytes();
        bytes.push(b'\n');
        result.insert(line_number, bytes);
    }
    result
}

/// Port of the full `build_interval_detours` layer loop (minus the G-code
/// parsing itself, already done by `segments::parse_wall_segments`): drives
/// `plan_segment`/`emit_plans`/the same-layer trim layer by layer, carrying
/// the `lower_*` state and (in "both" mode) `pending_upper_plans` forward
/// exactly as the Python closures do.
pub fn plan_interval_detours(layers: Vec<LayerSegments>, params: &OrchestrateParams) -> HashMap<usize, Vec<u8>> {
    let mut replacements: HashMap<usize, Vec<u8>> = HashMap::new();
    let mut lower_outer_grid: HashMap<GridKey, Vec<u32>> = HashMap::new();
    let mut lower_inner_grid: HashMap<GridKey, Vec<u32>> = HashMap::new();
    let mut lower_outer: Vec<Segment> = Vec::new();
    let mut lower_inner: Vec<Segment> = Vec::new();
    let mut pending_upper_plans: HashMap<usize, PlannedSegment> = HashMap::new();
    let mut layer: i64 = -1;

    for layer_segments in layers {
        let LayerSegments { outer, inner } = layer_segments;
        if outer.is_empty() {
            lower_outer_grid = HashMap::new();
            lower_inner_grid = HashMap::new();
            lower_outer = Vec::new();
            lower_inner = Vec::new();
            layer += 1;
            continue;
        }
        let mut outer = outer;
        let inner = inner;

        let current_display_layer = layer + 1;
        let lower_display_layer = layer;
        let current_grid = build_grid(&outer, params.plan.cell_mm);
        let current_inner_grid =
            if params.detour_side == DetourSide::Both { build_grid(&inner, params.plan.cell_mm) } else { HashMap::new() };

        match params.detour_side {
            DetourSide::Both => {
                let lower_should_plan = !lower_outer.is_empty()
                    && params.target_layers.as_ref().map_or(true, |t| t.contains(&lower_display_layer));
                let mut lower_plans: Vec<PlannedSegment> = Vec::new();
                if lower_should_plan {
                    for index in eligible_indices(&lower_outer, &params.subject_filaments) {
                        let plan = plan_segment(
                            &mut lower_outer[index],
                            &lower_inner_grid,
                            &lower_inner,
                            &current_grid,
                            &outer,
                            &params.plan,
                        );
                        lower_plans.push(PlannedSegment { segment: lower_outer[index].clone(), plan });
                    }
                }
                let pending: Vec<PlannedSegment> = pending_upper_plans.drain().map(|(_, v)| v).collect();
                let emitted = emit_plans(merge_plan_sets(vec![pending, lower_plans]));
                replacements.extend(emitted);

                let upper_should_plan = !lower_outer_grid.is_empty()
                    && params.target_layers.as_ref().map_or(true, |t| t.contains(&current_display_layer));
                let mut current_upper_plans: Vec<PlannedSegment> = Vec::new();
                if upper_should_plan {
                    for index in eligible_indices(&outer, &params.subject_filaments) {
                        let plan = plan_segment(
                            &mut outer[index],
                            &current_inner_grid,
                            &inner,
                            &lower_outer_grid,
                            &lower_outer,
                            &params.plan,
                        );
                        current_upper_plans.push(PlannedSegment { segment: outer[index].clone(), plan });
                    }
                }
                pending_upper_plans = current_upper_plans
                    .into_iter()
                    .filter(|p| !p.plan.spans.is_empty())
                    .map(|p| (p.segment.line_number, p))
                    .collect();
            }
            DetourSide::Upper => {
                let should_plan =
                    params.target_layers.as_ref().map_or(true, |t| t.contains(&current_display_layer));
                let subject_inner_grid = if should_plan { build_grid(&inner, params.plan.cell_mm) } else { HashMap::new() };
                let mut plans: Vec<PlannedSegment> = Vec::new();
                if should_plan && !lower_outer_grid.is_empty() {
                    for index in eligible_indices(&outer, &params.subject_filaments) {
                        let plan = plan_segment(
                            &mut outer[index],
                            &subject_inner_grid,
                            &inner,
                            &lower_outer_grid,
                            &lower_outer,
                            &params.plan,
                        );
                        if !plan.spans.is_empty() {
                            plans.push(PlannedSegment { segment: outer[index].clone(), plan });
                        }
                    }
                }
                let emitted = emit_plans(plans);
                replacements.extend(emitted);

                if params.same_layer_gap_mm > 0.0 && should_plan {
                    let trims = same_layer_channel_trims(
                        &outer,
                        params.same_layer_gap_mm,
                        params.same_layer_endpoint_tolerance_mm,
                        &replacements,
                    );
                    replacements.extend(trims);
                }
            }
            DetourSide::Lower => {
                let should_plan = !lower_outer.is_empty()
                    && params.target_layers.as_ref().map_or(true, |t| t.contains(&lower_display_layer));
                let subject_inner_grid =
                    if should_plan { build_grid(&lower_inner, params.plan.cell_mm) } else { HashMap::new() };
                let reference_grid = if should_plan { build_grid(&outer, params.plan.cell_mm) } else { HashMap::new() };
                let mut plans: Vec<PlannedSegment> = Vec::new();
                if should_plan && !reference_grid.is_empty() {
                    for index in eligible_indices(&lower_outer, &params.subject_filaments) {
                        let plan = plan_segment(
                            &mut lower_outer[index],
                            &subject_inner_grid,
                            &lower_inner,
                            &reference_grid,
                            &outer,
                            &params.plan,
                        );
                        if !plan.spans.is_empty() {
                            plans.push(PlannedSegment { segment: lower_outer[index].clone(), plan });
                        }
                    }
                }
                let emitted = emit_plans(plans);
                replacements.extend(emitted);
            }
        }

        lower_outer_grid = if matches!(params.detour_side, DetourSide::Upper | DetourSide::Both) {
            current_grid
        } else {
            HashMap::new()
        };
        lower_inner_grid = if params.detour_side == DetourSide::Both { current_inner_grid } else { HashMap::new() };
        lower_outer = outer;
        lower_inner = inner;
        layer += 1;
    }

    if params.detour_side == DetourSide::Both {
        let emitted = emit_plans(pending_upper_plans.into_values().collect());
        replacements.extend(emitted);
    }

    replacements
}
