//! Port of the wall-segment collection loop shared (duplicated verbatim) by
//! `gcode_layer_detour.py` and `gcode_layer_interval_detour.py`: parses the
//! raw sliced G-code into per-layer lists of "Outer wall"/"Inner wall" move
//! segments. This is step 2 of the port -- it stops at segment collection;
//! the spatial grid, neighbor search, and detour planning that consume these
//! segments are steps 3-4.

use crate::gcode::{
    first_token_upper, is_ascii_ws, scan_fields_bounded, split_lines_with_endings, strip, CP_END,
    CP_START, TOWER_END, TOWER_START,
};

#[derive(Debug, Clone, PartialEq)]
pub struct Segment {
    pub x1: f64,
    pub y1: f64,
    pub x2: f64,
    pub y2: f64,
    pub filament: i64,
    pub object_id: i64,
    pub line_number: usize,
    pub command: Vec<u8>,
    pub extrusion: f64,
    pub i: f64,
    pub j: f64,
    pub feed: Option<f64>,
    // Mirrors `Segment.__post_init__` in gcode_layer_detour.py -- derived
    // once at construction, not stored fields the caller passes in.
    pub dx: f64,
    pub dy: f64,
    pub length2: f64,
    pub length: f64,
    pub unit_x: f64,
    pub unit_y: f64,
}

impl Segment {
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        x1: f64,
        y1: f64,
        x2: f64,
        y2: f64,
        filament: i64,
        object_id: i64,
        line_number: usize,
        command: Vec<u8>,
        extrusion: f64,
        i: f64,
        j: f64,
        feed: Option<f64>,
    ) -> Self {
        let dx = x2 - x1;
        let dy = y2 - y1;
        let length2 = dx * dx + dy * dy;
        let length = length2.sqrt();
        let (unit_x, unit_y) = if length > 1e-12 { (dx / length, dy / length) } else { (0.0, 0.0) };
        Segment {
            x1,
            y1,
            x2,
            y2,
            filament,
            object_id,
            line_number,
            command,
            extrusion,
            i,
            j,
            feed,
            dx,
            dy,
            length2,
            length,
            unit_x,
            unit_y,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct LayerSegments {
    pub outer: Vec<Segment>,
    pub inner: Vec<Segment>,
}

fn is_g0_to_g3(command: &[u8]) -> bool {
    matches!(command, b"G0" | b"G1" | b"G2" | b"G3")
}

/// Mirrors `T_RE = re.compile(rb"^T([01])$")` (no `re.IGNORECASE` -- case
/// sensitive, requires an exact two-byte match).
fn match_tool_select(stripped: &[u8]) -> Option<i64> {
    if stripped.len() == 2 && stripped[0] == b'T' && matches!(stripped[1], b'0' | b'1') {
        Some((stripped[1] - b'0') as i64)
    } else {
        None
    }
}

/// Mirrors `OBJECT_RE = re.compile(rb"^; OBJECT_ID:\s*(\d+)")` (`.match`, so
/// trailing content after the digits is ignored).
fn match_object_id(stripped: &[u8]) -> Option<i64> {
    const PREFIX: &[u8] = b"; OBJECT_ID:";
    if !stripped.starts_with(PREFIX) {
        return None;
    }
    let mut i = PREFIX.len();
    while i < stripped.len() && is_ascii_ws(stripped[i]) {
        i += 1;
    }
    let digits_start = i;
    while i < stripped.len() && stripped[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return None;
    }
    std::str::from_utf8(&stripped[digits_start..i]).ok()?.parse::<i64>().ok()
}

/// Mirrors `FEATURE_RE = re.compile(rb"^; FEATURE:\s*(.*)")`: the rest of the
/// (already-stripped) line verbatim, case preserved, not further trimmed.
fn match_feature(stripped: &[u8]) -> Option<Vec<u8>> {
    const PREFIX: &[u8] = b"; FEATURE:";
    if !stripped.starts_with(PREFIX) {
        return None;
    }
    let mut i = PREFIX.len();
    while i < stripped.len() && is_ascii_ws(stripped[i]) {
        i += 1;
    }
    Some(stripped[i..].to_vec())
}

#[derive(Default)]
struct XyeijfFields {
    x: Option<f64>,
    y: Option<f64>,
    e: Option<f64>,
    i: Option<f64>,
    j: Option<f64>,
    f: Option<f64>,
}

/// Mirrors `{key: float(value) for key, value in FIELD_RE.findall(stripped)}`
/// -- a plain dict comprehension, so a repeated letter keeps its LAST value.
fn xyeijf_fields(stripped: &[u8]) -> XyeijfFields {
    let mut fields = XyeijfFields::default();
    for (letter, value) in scan_fields_bounded(stripped, b"XYEIJF") {
        match letter {
            b'X' => fields.x = Some(value),
            b'Y' => fields.y = Some(value),
            b'E' => fields.e = Some(value),
            b'I' => fields.i = Some(value),
            b'J' => fields.j = Some(value),
            b'F' => fields.f = Some(value),
            _ => {}
        }
    }
    fields
}

/// Port of the shared parsing loop. `filament_overrides` mirrors
/// `[(first_line, last_line, override_filament), ...]` -- the first matching
/// range (if any) wins, same as the Python `break`-on-first-match loop.
pub fn parse_wall_segments(gcode: &[u8], filament_overrides: &[(usize, usize, i64)]) -> Vec<LayerSegments> {
    let mut layers: Vec<LayerSegments> = Vec::new();
    let mut outer: Vec<Segment> = Vec::new();
    let mut inner: Vec<Segment> = Vec::new();
    let mut layer: i64 = -1;
    let mut filament: i64 = 0;
    let mut object_id: Option<i64> = None;
    let mut feature: Vec<u8> = Vec::new();
    let mut x = 0.0f64;
    let mut y = 0.0f64;
    let mut relative_xy = false;
    let mut in_cp = false;
    let mut in_tower = false;

    for (index, &raw) in split_lines_with_endings(gcode).iter().enumerate() {
        let line_number = index + 1;
        let stripped = strip(raw);

        if stripped == b"; CHANGE_LAYER" {
            layers.push(LayerSegments { outer: std::mem::take(&mut outer), inner: std::mem::take(&mut inner) });
            layer += 1;
            feature.clear();
            continue;
        }
        if let Some(value) = match_tool_select(stripped) {
            filament = value;
            continue;
        }
        if stripped == CP_START {
            in_cp = true;
            continue;
        }
        if stripped == CP_END {
            in_cp = false;
            continue;
        }
        if stripped == TOWER_START {
            in_tower = true;
            continue;
        }
        if stripped == TOWER_END {
            in_tower = false;
            continue;
        }
        if let Some(value) = match_object_id(stripped) {
            object_id = Some(value);
            continue;
        }
        if let Some(value) = match_feature(stripped) {
            feature = value;
            continue;
        }
        if stripped == b"G90" {
            relative_xy = false;
            continue;
        }
        if stripped == b"G91" {
            relative_xy = true;
            continue;
        }

        let command = first_token_upper(stripped);
        if !is_g0_to_g3(&command) {
            continue;
        }

        let fields = xyeijf_fields(stripped);
        let nx = if relative_xy { x + fields.x.unwrap_or(0.0) } else { fields.x.unwrap_or(x) };
        let ny = if relative_xy { y + fields.y.unwrap_or(0.0) } else { fields.y.unwrap_or(y) };
        let extrusion = fields.e.unwrap_or(0.0);

        if layer >= 0
            && !in_cp
            && !in_tower
            && object_id.is_some()
            && extrusion > 0.0
            && (nx - x).hypot(ny - y) > 1e-5
            && (feature == b"Outer wall" || feature == b"Inner wall")
        {
            let mut segment_filament = filament;
            for &(first_line, last_line, override_filament) in filament_overrides {
                if first_line <= line_number && line_number <= last_line {
                    segment_filament = override_filament;
                    break;
                }
            }
            let segment = Segment::new(
                x,
                y,
                nx,
                ny,
                segment_filament,
                object_id.unwrap(),
                line_number,
                command.clone(),
                extrusion,
                fields.i.unwrap_or(0.0),
                fields.j.unwrap_or(0.0),
                fields.f,
            );
            if feature == b"Outer wall" {
                outer.push(segment);
            } else {
                inner.push(segment);
            }
        }
        x = nx;
        y = ny;
    }
    layers.push(LayerSegments { outer, inner });
    layers
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn collects_outer_and_inner_wall_segments_per_layer() {
        let gcode = b"G1 X0 Y0\n\
; CHANGE_LAYER\n\
; OBJECT_ID: 1\n\
; FEATURE: Outer wall\n\
G1 X1 Y0 E.1\n\
G1 X1 Y1 E.1\n\
; FEATURE: Inner wall\n\
G1 X2 Y1 E.1\n\
; CHANGE_LAYER\n\
; FEATURE: Outer wall\n\
G1 X3 Y1 E.1\n";
        let layers = parse_wall_segments(gcode, &[]);
        // layer -1 (before the first CHANGE_LAYER) is always empty.
        assert_eq!(layers.len(), 3);
        assert!(layers[0].outer.is_empty() && layers[0].inner.is_empty());
        assert_eq!(layers[1].outer.len(), 2);
        assert_eq!(layers[1].inner.len(), 1);
        assert_eq!(layers[1].outer[0].x1, 0.0);
        assert_eq!(layers[1].outer[0].x2, 1.0);
        assert_eq!(layers[1].outer[0].object_id, 1);
        assert_eq!(layers[1].outer[0].command, b"G1");
        assert_eq!(layers[2].outer.len(), 1);
    }

    #[test]
    fn ignores_moves_inside_toolchange_or_tower_blocks() {
        let gcode = b"; CHANGE_LAYER\n\
; OBJECT_ID: 1\n\
; FEATURE: Outer wall\n\
; CP TOOLCHANGE START\n\
G1 X5 Y5 E.1\n\
; CP TOOLCHANGE END\n\
; WIPE_TOWER_START\n\
G1 X6 Y6 E.1\n\
; WIPE_TOWER_END\n\
G1 X1 Y1 E.1\n";
        let layers = parse_wall_segments(gcode, &[]);
        assert_eq!(layers[1].outer.len(), 1);
        assert_eq!(layers[1].outer[0].x2, 1.0);
    }

    #[test]
    fn applies_filament_overrides() {
        let gcode = b"T1\n\
; CHANGE_LAYER\n\
; OBJECT_ID: 1\n\
; FEATURE: Outer wall\n\
G1 X1 Y1 E.1\n";
        let layers = parse_wall_segments(gcode, &[(1, 10, 0)]);
        assert_eq!(layers[1].outer[0].filament, 0); // override wins over T1's filament=1
    }
}
