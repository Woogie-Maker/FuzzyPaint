mod emit;
mod gcode;
mod grid;
mod orchestrate;
mod planner;
mod segments;
mod single_filament;

use pyo3::exceptions::PyValueError;

/// A Python module implemented in Rust.
#[pyo3::pymodule]
mod fuzzypaint_engine {
    use pyo3::prelude::*;

    /// Python-visible mirror of `segments::Segment`, field-for-field (see
    /// `gcode_layer_detour.Segment`'s dataclass -- only the raw constructor
    /// fields, not its `__post_init__`-derived ones, which aren't needed
    /// until step 3's grid/neighbor-search port).
    #[pyclass(skip_from_py_object)]
    #[derive(Clone)]
    struct PySegment {
        #[pyo3(get)]
        x1: f64,
        #[pyo3(get)]
        y1: f64,
        #[pyo3(get)]
        x2: f64,
        #[pyo3(get)]
        y2: f64,
        #[pyo3(get)]
        filament: i64,
        #[pyo3(get)]
        object_id: i64,
        #[pyo3(get)]
        line_number: usize,
        #[pyo3(get)]
        command: Vec<u8>,
        #[pyo3(get)]
        extrusion: f64,
        #[pyo3(get)]
        i: f64,
        #[pyo3(get)]
        j: f64,
        #[pyo3(get)]
        feed: Option<f64>,
    }

    impl From<crate::segments::Segment> for PySegment {
        fn from(segment: crate::segments::Segment) -> Self {
            PySegment {
                x1: segment.x1,
                y1: segment.y1,
                x2: segment.x2,
                y2: segment.y2,
                filament: segment.filament,
                object_id: segment.object_id,
                line_number: segment.line_number,
                command: segment.command,
                extrusion: segment.extrusion,
                i: segment.i,
                j: segment.j,
                feed: segment.feed,
            }
        }
    }

    /// Port of the wall-segment parsing loop shared by `gcode_layer_detour.py`
    /// and `gcode_layer_interval_detour.py`. Returns one `(outer, inner)` pair
    /// per layer (including an always-empty leading pair for layer -1, before
    /// the first `; CHANGE_LAYER`, matching the Python loop's own indexing).
    ///
    /// `filament_overrides` mirrors `[(first_line, last_line, filament), ...]`;
    /// the first matching range wins.
    #[pyfunction]
    #[pyo3(signature = (gcode, filament_overrides=Vec::new()))]
    fn parse_wall_segments(
        gcode: &[u8],
        filament_overrides: Vec<(usize, usize, i64)>,
    ) -> Vec<(Vec<PySegment>, Vec<PySegment>)> {
        crate::segments::parse_wall_segments(gcode, &filament_overrides)
            .into_iter()
            .map(|layer| {
                (
                    layer.outer.into_iter().map(PySegment::from).collect(),
                    layer.inner.into_iter().map(PySegment::from).collect(),
                )
            })
            .collect()
    }

    /// Plain-tuple shape used to pass a `Segment` across the Python/Rust
    /// boundary for the step-3 grid/neighbor-search differential tests:
    /// `(x1, y1, x2, y2, filament, object_id, line_number, command, extrusion, i, j, feed)`.
    type SegmentTuple = (f64, f64, f64, f64, i64, i64, usize, Vec<u8>, f64, f64, f64, Option<f64>);

    fn segment_from_tuple(t: SegmentTuple) -> crate::segments::Segment {
        crate::segments::Segment::new(t.0, t.1, t.2, t.3, t.4, t.5, t.6, t.7, t.8, t.9, t.10, t.11)
    }

    /// Port of `_aligned`.
    #[pyfunction]
    #[pyo3(signature = (a, b, minimum=0.35))]
    fn aligned(a: SegmentTuple, b: SegmentTuple, minimum: f64) -> bool {
        crate::grid::aligned(&segment_from_tuple(a), &segment_from_tuple(b), minimum)
    }

    /// Port of `_closest_point`.
    #[pyfunction]
    fn closest_point(px: f64, py: f64, segment: SegmentTuple) -> (f64, f64, f64) {
        crate::grid::closest_point(px, py, &segment_from_tuple(segment))
    }

    /// Port of `_sample_points`.
    #[pyfunction]
    fn sample_points(segment: SegmentTuple, spacing: f64) -> Vec<(f64, f64)> {
        crate::grid::sample_points(&segment_from_tuple(segment), spacing)
    }

    /// Port of `_polyline_length`.
    #[pyfunction]
    fn polyline_length(points: Vec<(f64, f64)>) -> f64 {
        crate::grid::polyline_length(&points)
    }

    /// Builds a `_grid`-equivalent from `segments` and runs one `_nearby_point`
    /// query against it, returning the matching segments' `line_number`s in
    /// the same order the Python generator would yield them.
    #[pyfunction]
    fn nearby_segment_line_numbers(
        segments: Vec<SegmentTuple>,
        cell_mm: f64,
        object_id: i64,
        x: f64,
        y: f64,
        radius_mm: f64,
    ) -> Vec<usize> {
        let segments: Vec<crate::segments::Segment> = segments.into_iter().map(segment_from_tuple).collect();
        let grid = crate::grid::build_grid(&segments, cell_mm);
        crate::grid::nearby_point(&grid, &segments, object_id, x, y, radius_mm, cell_mm)
            .into_iter()
            .map(|index| segments[index].line_number)
            .collect()
    }

    /// Runs `plan_segment` exactly once for a freshly-constructed `subject`
    /// segment (no cache reuse across calls -- that's an internal memoization
    /// detail, invisible from the output), against grids built fresh from
    /// `inner_segments`/`reference_segments`. Returns
    /// `(original_points, [(original, shifted, continue_in, continue_out), ...])`.
    #[pyfunction]
    #[pyo3(signature = (
        subject, inner_segments, reference_segments, *,
        inset_mm=0.24, boundary_resolution_mm=0.10, transition_distance_mm=0.55,
        endpoint_coverage_mm=0.21, inner_search_mm=1.2, minimum_span_mm=0.10, cell_mm=0.55
    ))]
    #[allow(clippy::too_many_arguments)]
    fn plan_segment_once(
        subject: SegmentTuple,
        inner_segments: Vec<SegmentTuple>,
        reference_segments: Vec<SegmentTuple>,
        inset_mm: f64,
        boundary_resolution_mm: f64,
        transition_distance_mm: f64,
        endpoint_coverage_mm: f64,
        inner_search_mm: f64,
        minimum_span_mm: f64,
        cell_mm: f64,
    ) -> (Vec<(f64, f64)>, Vec<(Vec<(f64, f64)>, Vec<(f64, f64)>, bool, bool)>) {
        let mut subject = segment_from_tuple(subject);
        let inner_segments: Vec<crate::segments::Segment> =
            inner_segments.into_iter().map(segment_from_tuple).collect();
        let reference_segments: Vec<crate::segments::Segment> =
            reference_segments.into_iter().map(segment_from_tuple).collect();
        let params = crate::planner::PlanParams {
            inset_mm,
            boundary_resolution_mm,
            transition_distance_mm,
            endpoint_coverage_mm,
            inner_search_mm,
            minimum_span_mm,
            cell_mm,
        };
        let inner_grid = crate::grid::build_grid(&inner_segments, cell_mm);
        let reference_grid = crate::grid::build_grid(&reference_segments, cell_mm);
        let plan = crate::planner::plan_segment(
            &mut subject,
            &inner_grid,
            &inner_segments,
            &reference_grid,
            &reference_segments,
            &params,
        );
        let spans = plan
            .spans
            .into_iter()
            .map(|span| (span.original, span.shifted, span.continue_in, span.continue_out))
            .collect();
        (plan.original, spans)
    }

    /// Mirrors Python's `f"{value:g}"`.
    #[pyfunction]
    fn format_g(value: f64) -> String {
        crate::emit::format_g(value)
    }

    /// Port of `_rdp_indices`.
    #[pyfunction]
    fn rdp_indices(points: Vec<(f64, f64)>, tolerance: f64) -> Vec<usize> {
        crate::emit::rdp_indices(&points, tolerance)
    }

    /// Port of `_emit_plan`, taking a plan directly (not re-derived from
    /// `plan_segment`) -- `spans` is `[(original, shifted, continue_in, continue_out), ...]`.
    #[pyfunction]
    fn emit_plan(
        segment: SegmentTuple,
        original: Vec<(f64, f64)>,
        spans: Vec<(Vec<(f64, f64)>, Vec<(f64, f64)>, bool, bool)>,
    ) -> Vec<u8> {
        let segment = segment_from_tuple(segment);
        let plan = crate::planner::SegmentPlan {
            original,
            spans: spans
                .into_iter()
                .map(|(original, shifted, continue_in, continue_out)| crate::planner::Span {
                    original,
                    shifted,
                    continue_in,
                    continue_out,
                })
                .collect(),
        };
        crate::emit::emit_plan(plan, &segment)
    }

    /// Runs `plan_segment` once (same scope as `plan_segment_once`) and emits
    /// the resulting plan to G-code text via `_emit_plan`'s port, in one call
    /// -- this is the real step-4-into-step-5 pipeline for a single segment.
    #[pyfunction]
    #[pyo3(signature = (
        subject, inner_segments, reference_segments, *,
        inset_mm=0.24, boundary_resolution_mm=0.10, transition_distance_mm=0.55,
        endpoint_coverage_mm=0.21, inner_search_mm=1.2, minimum_span_mm=0.10, cell_mm=0.55
    ))]
    #[allow(clippy::too_many_arguments)]
    fn plan_and_emit_segment(
        subject: SegmentTuple,
        inner_segments: Vec<SegmentTuple>,
        reference_segments: Vec<SegmentTuple>,
        inset_mm: f64,
        boundary_resolution_mm: f64,
        transition_distance_mm: f64,
        endpoint_coverage_mm: f64,
        inner_search_mm: f64,
        minimum_span_mm: f64,
        cell_mm: f64,
    ) -> Vec<u8> {
        let mut subject_segment = segment_from_tuple(subject);
        let inner_segments: Vec<crate::segments::Segment> =
            inner_segments.into_iter().map(segment_from_tuple).collect();
        let reference_segments: Vec<crate::segments::Segment> =
            reference_segments.into_iter().map(segment_from_tuple).collect();
        let params = crate::planner::PlanParams {
            inset_mm,
            boundary_resolution_mm,
            transition_distance_mm,
            endpoint_coverage_mm,
            inner_search_mm,
            minimum_span_mm,
            cell_mm,
        };
        let inner_grid = crate::grid::build_grid(&inner_segments, cell_mm);
        let reference_grid = crate::grid::build_grid(&reference_segments, cell_mm);
        let plan = crate::planner::plan_segment(
            &mut subject_segment,
            &inner_grid,
            &inner_segments,
            &reference_grid,
            &reference_segments,
            &params,
        );
        crate::emit::emit_plan(plan, &subject_segment)
    }

    /// Full port of `build_interval_detours` (parsing + planning +
    /// emission for an entire sliced G-code buffer, all three
    /// `detour_side` modes). Returns `{line_number: replacement_bytes}`,
    /// exactly matching the Python function's return value -- this is
    /// the single entry point step 5's finalize_layer/emit_plans
    /// orchestration was ported for.
    #[pyfunction]
    #[pyo3(signature = (
        gcode, *,
        inset_mm=0.24, boundary_resolution_mm=0.10, transition_distance_mm=0.55,
        endpoint_coverage_mm=0.21, inner_search_mm=1.2, minimum_span_mm=0.10,
        same_layer_gap_mm=0.08, same_layer_endpoint_tolerance_mm=0.65,
        detour_side="upper".to_string(), target_layers=None, subject_filaments=None,
        filament_overrides=Vec::new(), cell_mm=0.55
    ))]
    #[allow(clippy::too_many_arguments)]
    fn build_interval_detours<'py>(
        py: Python<'py>,
        gcode: &[u8],
        inset_mm: f64,
        boundary_resolution_mm: f64,
        transition_distance_mm: f64,
        endpoint_coverage_mm: f64,
        inner_search_mm: f64,
        minimum_span_mm: f64,
        same_layer_gap_mm: f64,
        same_layer_endpoint_tolerance_mm: f64,
        detour_side: String,
        target_layers: Option<std::collections::HashSet<i64>>,
        subject_filaments: Option<std::collections::HashSet<i64>>,
        filament_overrides: Vec<(usize, usize, i64)>,
        cell_mm: f64,
    ) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let side = match detour_side.as_str() {
            "upper" => crate::orchestrate::DetourSide::Upper,
            "lower" => crate::orchestrate::DetourSide::Lower,
            "both" => crate::orchestrate::DetourSide::Both,
            other => {
                return Err(super::PyValueError::new_err(format!(
                    "detour_side must be 'upper', 'lower', or 'both', got {:?}",
                    other
                )))
            }
        };
        let layers = crate::segments::parse_wall_segments(gcode, &filament_overrides);
        let params = crate::orchestrate::OrchestrateParams {
            plan: crate::planner::PlanParams {
                inset_mm,
                boundary_resolution_mm,
                transition_distance_mm,
                endpoint_coverage_mm,
                inner_search_mm,
                minimum_span_mm,
                cell_mm,
            },
            detour_side: side,
            target_layers,
            subject_filaments,
            same_layer_gap_mm,
            same_layer_endpoint_tolerance_mm,
        };
        let replacements = crate::orchestrate::plan_interval_detours(layers, &params);
        let result = pyo3::types::PyDict::new(py);
        for (line_number, bytes) in replacements {
            result.set_item(line_number, bytes)?;
        }
        Ok(result)
    }

    /// Proves the Rust/Python round-trip end-to-end (see memory/
    /// fuzzypaint-rust-port-strategy.md step 0). Not used by the real engine.
    #[pyfunction]
    fn sum_as_string(a: usize, b: usize) -> PyResult<String> {
        Ok((a + b).to_string())
    }

    /// Port of `gcode_single_filament._find_tower_excursion_ranges`.
    #[pyfunction]
    fn find_tower_excursion_ranges(gcode: &[u8]) -> Vec<(usize, usize)> {
        crate::single_filament::find_tower_excursion_ranges(gcode)
    }

    /// Port of `gcode_single_filament._strip_marked_blocks`, scoped to tool-
    /// change/tower-block removal and the tower-excursion skip ranges (see
    /// the module doc comment in `single_filament.rs` for what's excluded).
    ///
    /// Returns `(output_gcode, report_dict)`. Raises `ValueError` on the same
    /// conditions the Python version raises `ConversionError` for -- exact
    /// exception-type parity is deferred to when this is wired into the real
    /// pipeline (step 5 of the port).
    #[pyfunction]
    fn strip_marked_blocks<'py>(
        py: Python<'py>,
        gcode: &[u8],
        skip_ranges: Vec<(usize, usize)>,
    ) -> PyResult<(Vec<u8>, Bound<'py, pyo3::types::PyDict>)> {
        match crate::single_filament::strip_marked_blocks(gcode, &skip_ranges) {
            Ok(result) => {
                let report = pyo3::types::PyDict::new(py);
                report.set_item("toolchanges_removed", result.toolchanges_removed)?;
                report.set_item("tower_blocks_removed", result.tower_blocks_removed)?;
                report.set_item("kept_motion_lines", result.kept_motion_lines)?;
                report.set_item("filament_length_mm", result.filament_length_mm)?;
                report.set_item("orphan_unretractions_removed", result.orphan_unretractions_removed)?;
                Ok((result.output, report))
            }
            Err(message) => Err(super::PyValueError::new_err(message)),
        }
    }
}
