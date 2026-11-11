mod gcode;
mod grid;
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
