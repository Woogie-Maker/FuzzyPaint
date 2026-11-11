mod gcode;
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
