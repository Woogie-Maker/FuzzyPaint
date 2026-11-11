//! Port of the plan-to-G-code emission in `gcode_layer_interval_detour.py`:
//! `_rdp_indices`, `_simplify_paired`, `_line_to`, `_travel_to`, `_emit_plan`.
//! Step 5 of the port. Unlike step 4's targets, these are real importable
//! module-level functions, so differential tests call production code
//! directly.

use crate::grid::{distance, polyline_length, same_point};
use crate::planner::SegmentPlan;
use crate::segments::Segment;

/// Port of `_rdp_indices` (Ramer-Douglas-Peucker simplification, returning
/// kept indices rather than points).
pub fn rdp_indices(points: &[(f64, f64)], tolerance: f64) -> Vec<usize> {
    if points.len() <= 2 {
        return if points.len() == 2 { vec![0, 1] } else { vec![0] };
    }
    let (ax, ay) = points[0];
    let (bx, by) = points[points.len() - 1];
    let dx = bx - ax;
    let dy = by - ay;
    let length2 = dx * dx + dy * dy;
    let mut best_index = 0usize;
    let mut best_distance = -1.0f64;
    for (offset, &(px, py)) in points[1..points.len() - 1].iter().enumerate() {
        let index = offset + 1;
        let candidate_distance = if length2 < 1e-12 {
            (px - ax).hypot(py - ay)
        } else {
            let t = (0.0f64).max((1.0f64).min(((px - ax) * dx + (py - ay) * dy) / length2));
            (px - (ax + t * dx)).hypot(py - (ay + t * dy))
        };
        if candidate_distance > best_distance {
            best_index = index;
            best_distance = candidate_distance;
        }
    }
    if best_distance <= tolerance {
        return vec![0, points.len() - 1];
    }
    let left = rdp_indices(&points[..=best_index], tolerance);
    let right = rdp_indices(&points[best_index..], tolerance);
    let mut result = left[..left.len() - 1].to_vec();
    result.extend(right.into_iter().map(|index| best_index + index));
    result
}

/// Port of `_simplify_paired`.
pub fn simplify_paired(
    original: &[(f64, f64)],
    shifted: &[(f64, f64)],
    tolerance: f64,
) -> (Vec<(f64, f64)>, Vec<(f64, f64)>) {
    let mut indices: std::collections::BTreeSet<usize> = rdp_indices(original, tolerance).into_iter().collect();
    indices.extend(rdp_indices(shifted, tolerance));
    let indices: Vec<usize> = indices.into_iter().collect(); // BTreeSet iterates in sorted order
    (indices.iter().map(|&i| original[i]).collect(), indices.iter().map(|&i| shifted[i]).collect())
}

/// Mirrors Python's `f"{value:g}"` (the default-precision `%g` format: 6
/// significant digits, trailing zeros and an unnecessary decimal point
/// stripped, switching to scientific notation outside [1e-4, 1e6)). Used
/// only for feed rates, which in practice never approach that range, but
/// implemented to the real rule rather than assumed.
pub fn format_g(value: f64) -> String {
    if value == 0.0 {
        return "0".to_string();
    }
    const PRECISION: i32 = 6;
    let exponent = value.abs().log10().floor() as i32;
    if exponent < -4 || exponent >= PRECISION {
        let digits_after = (PRECISION - 1).max(0) as usize;
        let formatted = format!("{:.*e}", digits_after, value);
        let (mantissa, exponent_str) = formatted.split_once('e').unwrap();
        let mantissa = trim_trailing_zeros(mantissa);
        let exp: i32 = exponent_str.parse().unwrap();
        format!("{}e{}{:02}", mantissa, if exp >= 0 { "+" } else { "-" }, exp.abs())
    } else {
        let digits_after = (PRECISION - 1 - exponent).max(0) as usize;
        let fixed = format!("{:.*}", digits_after, value);
        trim_trailing_zeros(&fixed)
    }
}

fn trim_trailing_zeros(s: &str) -> String {
    if !s.contains('.') {
        return s.to_string();
    }
    s.trim_end_matches('0').trim_end_matches('.').to_string()
}

/// Port of `_line_to`.
pub fn line_to(point: (f64, f64), extrusion: f64, comment: &str) -> String {
    let suffix = if !comment.is_empty() { format!(" ; {}", comment) } else { String::new() };
    format!("G1 X{:.5} Y{:.5} E{:.6}{}", point.0, point.1, extrusion, suffix)
}

/// Port of `_travel_to`.
pub fn travel_to(point: (f64, f64), comment: &str) -> String {
    let suffix = if !comment.is_empty() { format!(" ; {}", comment) } else { String::new() };
    format!("G0 X{:.5} Y{:.5}{}", point.0, point.1, suffix)
}

fn emit_line(point: (f64, f64), extrusion: f64, comment: &str, feed_pending: &mut Option<f64>) -> String {
    let mut line = line_to(point, extrusion, comment);
    if let Some(feed) = feed_pending.take() {
        if let Some(pos) = line.find(" ;") {
            line.replace_range(pos..pos + 2, &format!(" F{} ;", format_g(feed)));
        } else {
            line.push_str(&format!(" F{}", format_g(feed)));
        }
    }
    line
}

/// Mirrors the `elif len(unchanged) > 2: ... else: unchanged` fallthrough
/// combined with the `segment.command == b"G1"` collapse-to-endpoints case.
fn simplify_unchanged(points: &[(f64, f64)], is_g1: bool) -> Vec<(f64, f64)> {
    if is_g1 && points.len() > 1 {
        vec![points[0], points[points.len() - 1]]
    } else if points.len() > 2 {
        rdp_indices(points, 0.01).into_iter().map(|i| points[i]).collect()
    } else {
        points.to_vec()
    }
}

/// Port of `_emit_plan`. Takes `plan` by value since Python's version
/// mutates `span.original`/`span.shifted` in place via `_simplify_paired`
/// (that mutation is never observed again afterward in the real pipeline --
/// `_emit_plan` is always the last thing done with a given plan -- so a
/// local, consumed copy is equivalent).
pub fn emit_plan(mut plan: SegmentPlan, segment: &Segment) -> Vec<u8> {
    for span in plan.spans.iter_mut() {
        if span.original.len() > 2 {
            let (new_original, new_shifted) = simplify_paired(&span.original, &span.shifted, 0.01);
            span.original = new_original;
            span.shifted = new_shifted;
        }
    }
    let original_length = polyline_length(&plan.original).max(1e-9);
    let e_per_mm = segment.extrusion / original_length;
    let mut emitted: Vec<String> = Vec::new();
    let mut cursor_index = 0usize;
    let mut feed_pending = segment.feed;
    let is_g1 = segment.command == b"G1";

    for span in &plan.spans {
        let start = span.original[0];
        let end = *span.original.last().unwrap();
        let start_index = (cursor_index..plan.original.len()).find(|&i| plan.original[i] == start).unwrap();
        let end_index = (start_index..plan.original.len()).find(|&i| plan.original[i] == end).unwrap();

        let unchanged_prefix = simplify_unchanged(&plan.original[cursor_index..=start_index], is_g1);
        for pair in unchanged_prefix.windows(2) {
            emitted.push(emit_line(
                pair[1],
                distance(pair[0], pair[1]) * e_per_mm,
                "FuzzyPaint unchanged prefix",
                &mut feed_pending,
            ));
        }

        if !span.continue_in && !same_point(start, span.shifted[0], 1e-5) {
            emitted.push(travel_to(span.shifted[0], "FuzzyPaint tapered detour in"));
        }
        let shifted_length = polyline_length(&span.shifted).max(1e-9);
        let span_extrusion = polyline_length(&span.original) * e_per_mm;
        for pair in span.shifted.windows(2) {
            let extrusion = span_extrusion * distance(pair[0], pair[1]) / shifted_length;
            emitted.push(emit_line(pair[1], extrusion, "FuzzyPaint clipped inset span", &mut feed_pending));
        }
        if !span.continue_out && !same_point(*span.shifted.last().unwrap(), end, 1e-5) {
            emitted.push(travel_to(end, "FuzzyPaint tapered detour out"));
        }
        cursor_index = end_index;
    }

    let unchanged_suffix = simplify_unchanged(&plan.original[cursor_index..], is_g1);
    for pair in unchanged_suffix.windows(2) {
        emitted.push(emit_line(
            pair[1],
            distance(pair[0], pair[1]) * e_per_mm,
            "FuzzyPaint unchanged suffix",
            &mut feed_pending,
        ));
    }

    let mut result = emitted.join("\n");
    result.push('\n');
    result.into_bytes()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::planner::Span;

    #[test]
    fn rdp_collapses_nearly_straight_points() {
        let points = vec![(0.0, 0.0), (1.0, 0.001), (2.0, 0.0)];
        assert_eq!(rdp_indices(&points, 0.01), vec![0, 2]);
    }

    #[test]
    fn rdp_keeps_a_real_corner() {
        let points = vec![(0.0, 0.0), (1.0, 1.0), (2.0, 0.0)];
        assert_eq!(rdp_indices(&points, 0.01), vec![0, 1, 2]);
    }

    #[test]
    fn format_g_strips_trailing_zeros() {
        assert_eq!(format_g(1800.0), "1800");
        assert_eq!(format_g(3000.0), "3000");
        assert_eq!(format_g(0.5), "0.5");
        assert_eq!(format_g(100.25), "100.25");
    }

    #[test]
    fn line_to_matches_expected_precision() {
        assert_eq!(line_to((1.0, 2.5), 0.123456789, "note"), "G1 X1.00000 Y2.50000 E0.123457 ; note");
        assert_eq!(travel_to((1.0, 2.5), "note"), "G0 X1.00000 Y2.50000 ; note");
    }

    #[test]
    fn emit_plan_inserts_feed_once_on_first_line() {
        let segment = Segment::new(0.0, 0.0, 2.0, 0.0, 0, 1, 10, b"G1".to_vec(), 0.2, 0.0, 0.0, Some(1800.0));
        let plan = SegmentPlan {
            original: vec![(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)],
            spans: vec![Span {
                original: vec![(1.0, 0.0), (2.0, 0.0)],
                shifted: vec![(1.0, 0.1), (2.0, 0.1)],
                continue_in: false,
                continue_out: false,
            }],
        };
        let output = String::from_utf8(emit_plan(plan, &segment)).unwrap();
        assert!(output.contains("F1800"));
        assert_eq!(output.matches("F1800").count(), 1);
        assert!(output.ends_with('\n'));
    }
}
