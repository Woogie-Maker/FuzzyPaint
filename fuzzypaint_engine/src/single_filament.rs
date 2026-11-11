//! Port of the purely mechanical byte/line scanning in Python's
//! `gcode_single_filament.py`: finding the unmarked travel wrapper around a
//! Bambu wipe-tower excursion, and removing the marked tool-change/tower
//! blocks. No geometry, no grid -- lowest-risk first step of the engine port.
//!
//! Scope deliberately excludes `groove_lines`/`coordinate_overrides`/
//! `extrusion_scales`/`line_replacements` (the Python function's other
//! parameters): those exist to apply line replacements produced by the
//! detour-planning stage, which hasn't been ported yet (step 4-5 of the plan).
//! The existing Python unit tests already exercise `_strip_marked_blocks`
//! with those left at their defaults, which is exactly this scope.

use crate::gcode::{find_e_value_bounded, first_token_upper, is_move_command, match_pure_e, scan_fields, split_lines_with_endings, strip};

const CP_START: &[u8] = b"; CP TOOLCHANGE START";
const CP_END: &[u8] = b"; CP TOOLCHANGE END";
const TOWER_START: &[u8] = b"; WIPE_TOWER_START";
const TOWER_END: &[u8] = b"; WIPE_TOWER_END";

#[derive(Clone, Copy)]
struct Bounds {
    min_x: f64,
    max_x: f64,
    min_y: f64,
    max_y: f64,
}

impl Bounds {
    fn from_point(x: f64, y: f64) -> Self {
        Bounds { min_x: x, max_x: x, min_y: y, max_y: y }
    }

    fn grow(&mut self, x: f64, y: f64) {
        self.min_x = self.min_x.min(x);
        self.max_x = self.max_x.max(x);
        self.min_y = self.min_y.min(y);
        self.max_y = self.max_y.max(y);
    }

    fn padded(&self, margin: f64) -> Bounds {
        Bounds {
            min_x: self.min_x - margin,
            max_x: self.max_x + margin,
            min_y: self.min_y - margin,
            max_y: self.max_y + margin,
        }
    }

    fn contains(&self, x: f64, y: f64) -> bool {
        x >= self.min_x && x <= self.max_x && y >= self.min_y && y <= self.max_y
    }
}

/// `(x_field, y_field, e_field)` extracted from a G0-G3 move line, keeping only
/// the LAST occurrence of each letter -- matches how
/// `{key.upper(): float(value) for key, value in ...}` resolves duplicate keys.
fn xye_fields(stripped: &[u8]) -> (Option<f64>, Option<f64>, Option<f64>) {
    let mut x = None;
    let mut y = None;
    let mut e = None;
    for (letter, value) in scan_fields(stripped, b"XYE") {
        match letter {
            b'X' => x = Some(value),
            b'Y' => y = Some(value),
            b'E' => e = Some(value),
            _ => {}
        }
    }
    (x, y, e)
}

fn is_g0_to_g3(command: &[u8]) -> bool {
    matches!(command, b"G0" | b"G1" | b"G2" | b"G3")
}

/// Port of `_find_tower_excursion_ranges`.
pub fn find_tower_excursion_ranges(gcode: &[u8]) -> Vec<(usize, usize)> {
    let lines = split_lines_with_endings(gcode);

    // Pass 1: learn the tower's and the model's actual XY footprint.
    let mut tower_bounds: Option<Bounds> = None;
    let mut model_bounds: Option<Bounds> = None;
    {
        let mut x = 0.0f64;
        let mut y = 0.0f64;
        let mut relative_xy = false;
        let mut in_cp = false;
        let mut in_tower = false;
        let mut feature: Vec<u8> = Vec::new();
        let mut have_object = false;

        for &raw in &lines {
            let stripped = strip(raw);
            if stripped == CP_START {
                in_cp = true;
            } else if stripped == CP_END {
                in_cp = false;
            } else if stripped == TOWER_START && !in_cp {
                in_tower = true;
            } else if stripped == TOWER_END && !in_cp {
                in_tower = false;
            } else if stripped.starts_with(b"; OBJECT_ID:") && !in_cp {
                have_object = true;
            }
            if stripped.starts_with(b"; FEATURE:") {
                if let Some(colon) = stripped.iter().position(|&b| b == b':') {
                    feature = strip(&stripped[colon + 1..]).to_ascii_lowercase();
                }
            }
            if stripped == b"G90" {
                relative_xy = false;
            } else if stripped == b"G91" {
                relative_xy = true;
            }
            let command = first_token_upper(stripped);
            if is_g0_to_g3(&command) {
                let (fx, fy, fe) = xye_fields(stripped);
                let nx = if relative_xy { x + fx.unwrap_or(0.0) } else { fx.unwrap_or(x) };
                let ny = if relative_xy { y + fy.unwrap_or(0.0) } else { fy.unwrap_or(y) };
                if in_tower && (fx.is_some() || fy.is_some()) {
                    match &mut tower_bounds {
                        Some(b) => b.grow(nx, ny),
                        None => tower_bounds = Some(Bounds::from_point(nx, ny)),
                    }
                }
                if have_object
                    && !in_cp
                    && !in_tower
                    && feature != b"prime tower"
                    && fe.unwrap_or(0.0) > 0.0
                    && (fx.is_some() || fy.is_some())
                {
                    for &(px, py) in &[(x, y), (nx, ny)] {
                        match &mut model_bounds {
                            Some(b) => b.grow(px, py),
                            None => model_bounds = Some(Bounds::from_point(px, py)),
                        }
                    }
                }
                x = nx;
                y = ny;
            }
        }
    }
    let tower_bounds = tower_bounds.map(|b| b.padded(10.0));
    let model_bounds = model_bounds.map(|b| b.padded(10.0));

    // Pass 2: find the unmarked departure-and-return wrapper around each
    // standalone tower excursion.
    let mut ranges: Vec<(usize, usize)> = Vec::new();
    let mut x = 0.0f64;
    let mut y = 0.0f64;
    let mut relative_xy = false;
    let mut in_cp = false;
    let mut in_tower = false;
    let mut candidate_start: Option<usize> = None;
    let mut active_start: Option<usize> = None;

    for (index, &raw) in lines.iter().enumerate() {
        let line_number = index + 1;
        let stripped = strip(raw);

        if stripped == CP_START {
            if active_start.is_none() {
                if let Some(c) = candidate_start {
                    active_start = Some(c);
                }
            }
            in_cp = true;
        }
        if stripped == TOWER_START && !in_cp {
            in_tower = true;
        } else if stripped == TOWER_END && !in_cp {
            in_tower = false;
        }
        if active_start.is_some() && !in_cp && stripped.starts_with(b"; OBJECT_ID:") {
            ranges.push((active_start.unwrap(), line_number - 1));
            active_start = None;
            candidate_start = None;
        }
        if stripped == TOWER_START && !in_cp && active_start.is_none() {
            if let Some(c) = candidate_start {
                active_start = Some(c);
            }
        }
        if stripped == b"G90" {
            relative_xy = false;
        } else if stripped == b"G91" {
            relative_xy = true;
        }
        let command = first_token_upper(stripped);
        if is_g0_to_g3(&command) {
            let (fx, fy, fe) = xye_fields(stripped);
            let nx = if relative_xy { x + fx.unwrap_or(0.0) } else { fx.unwrap_or(x) };
            let ny = if relative_xy { y + fy.unwrap_or(0.0) } else { fy.unwrap_or(y) };

            let near_tower = tower_bounds.map_or(false, |b| b.contains(nx, ny));
            let outside_model = model_bounds.map_or(false, |b| !b.contains(nx, ny));

            if active_start.is_none()
                && !in_cp
                && !in_tower
                && fe.unwrap_or(0.0) <= 0.0
                && (outside_model
                    || (model_bounds.is_none() && near_tower && (nx - x).hypot(ny - y) > 30.0))
            {
                if candidate_start.is_none() {
                    candidate_start = Some(line_number);
                }
            } else if active_start.is_none()
                && !in_cp
                && !in_tower
                && fe.unwrap_or(0.0) > 0.0
                && (fx.is_some() || fy.is_some())
            {
                candidate_start = None;
            }
            x = nx;
            y = ny;
        }
        if stripped == CP_END {
            in_cp = false;
        }
    }
    ranges
}

pub struct StripResult {
    pub output: Vec<u8>,
    pub toolchanges_removed: u64,
    pub tower_blocks_removed: u64,
    pub kept_motion_lines: u64,
    pub filament_length_mm: f64,
    pub orphan_unretractions_removed: u64,
}

/// Port of `_strip_marked_blocks`, scoped to its tool-change/tower removal,
/// `skip_ranges` (tower-excursion) handling, and orphan-unretraction guard --
/// see the module doc comment for what's deliberately left out for now.
pub fn strip_marked_blocks(gcode: &[u8], skip_ranges: &[(usize, usize)]) -> Result<StripResult, String> {
    let mut in_cp = false;
    let mut in_tower = false;
    let mut range_iter = skip_ranges.iter();
    let mut current_range: Option<(usize, usize)> = range_iter.next().copied();
    let mut current_range_e = 0.0f64;
    let mut pending_unretract = false;

    let mut toolchanges_removed = 0u64;
    let mut tower_blocks_removed = 0u64;
    let mut kept_motion_lines = 0u64;
    let mut filament_length_mm = 0.0f64;
    let mut orphan_unretractions_removed = 0u64;
    let mut output: Vec<u8> = Vec::with_capacity(gcode.len());

    for (index, &line) in split_lines_with_endings(gcode).iter().enumerate() {
        let line_number = index + 1;
        let stripped = strip(line);

        if stripped == CP_START {
            if in_cp || in_tower {
                return Err("Nested or overlapping tool-change markers".to_string());
            }
            in_cp = true;
            toolchanges_removed += 1;
            continue;
        }
        if stripped == CP_END {
            if !in_cp {
                return Err("Tool-change end marker without a start marker".to_string());
            }
            in_cp = false;
            continue;
        }
        if in_cp {
            continue;
        }
        if stripped == TOWER_START {
            if in_tower {
                return Err("Nested wipe-tower markers".to_string());
            }
            in_tower = true;
            tower_blocks_removed += 1;
            continue;
        }
        if stripped == TOWER_END {
            if !in_tower {
                return Err("Wipe-tower end marker without a start marker".to_string());
            }
            in_tower = false;
            continue;
        }
        if in_tower {
            continue;
        }

        while let Some(range) = current_range {
            if line_number > range.1 {
                pending_unretract = current_range_e < -0.1;
                current_range_e = 0.0;
                current_range = range_iter.next().copied();
            } else {
                break;
            }
        }
        if let Some(range) = current_range {
            if range.0 <= line_number && line_number <= range.1 {
                if let Some(value) = find_e_value_bounded(stripped) {
                    current_range_e += value;
                }
                continue;
            }
        }

        if pending_unretract {
            if let Some(value) = match_pure_e(stripped) {
                if value > 0.0 {
                    pending_unretract = false;
                    orphan_unretractions_removed += 1;
                    continue;
                }
            }
            if stripped.starts_with(b"; FEATURE:")
                || (is_move_command(stripped) && find_e_value_bounded(stripped).is_some())
            {
                return Err(
                    "A removed tower trip was not followed by its expected unretraction".to_string(),
                );
            }
        }

        output.extend_from_slice(line);

        // Equivalent to Python's `for emitted in line.splitlines(): ...`: in
        // this scope `line` is always exactly the one original source line
        // (no `line_replacements` substitution can introduce embedded
        // newlines yet), so it reduces to checking `stripped` once.
        if is_move_command(stripped) {
            kept_motion_lines += 1;
            if let Some(value) = find_e_value_bounded(stripped) {
                filament_length_mm += value;
            }
        }
    }

    if in_cp || in_tower {
        return Err("The G-code ends inside a marked tool-change/tower block".to_string());
    }
    if toolchanges_removed == 0 {
        return Err("No Bambu tool-change blocks were found".to_string());
    }

    Ok(StripResult {
        output,
        toolchanges_removed,
        tower_blocks_removed,
        kept_motion_lines,
        filament_length_mm,
        orphan_unretractions_removed,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_unmarked_travel_wrapping_a_wipe_tower() {
        let gcode = b"G90\n\
G1 X100 Y130\n\
; WIPE_END\n\
G1 X220 Y220\n\
; FEATURE: Prime tower\n\
; WIPE_TOWER_START\n\
G1 X225 Y225 E1\n\
; WIPE_TOWER_END\n\
; CP TOOLCHANGE START\n\
G1 X220 Y220\n\
; CP TOOLCHANGE END\n\
; WIPE_START\n\
G1 X219 Y220 E-.8\n\
; WIPE_END\n\
; OBJECT_ID: 1\n\
G1 X101 Y131\n";
        assert_eq!(find_tower_excursion_ranges(gcode), vec![(4, 14)]);
    }

    #[test]
    fn strips_marked_blocks_and_counts_report_fields() {
        let gcode = b"; CP TOOLCHANGE START\n\
G1 E-2\n\
; CP TOOLCHANGE END\n\
G1 X220 Y220\n\
; WIPE_START\n\
G1 X219 Y220 E-.76\n\
; WIPE_END\n\
G1 E-.04 F1800\n\
; OBJECT_ID: 1\n\
G1 X100 Y100 F30000\n\
G1 Z.2\n\
G1 E.8 F1800\n\
; FEATURE: Outer wall\n\
G1 X101 Y100 E.1\n";
        let result = strip_marked_blocks(gcode, &[(4, 8)]).unwrap();
        let text = String::from_utf8(result.output.clone()).unwrap();
        assert!(text.contains("G1 X100 Y100 F30000"));
        assert!(!text.contains("G1 E.8 F1800"));
        assert!(text.contains("G1 X101 Y100 E.1"));
        assert_eq!(result.orphan_unretractions_removed, 1);
        assert_eq!(result.toolchanges_removed, 1);
    }

    #[test]
    fn travel_only_tower_range_keeps_following_unretraction() {
        let gcode = b"; CP TOOLCHANGE START\n\
G1 E-2\n\
; CP TOOLCHANGE END\n\
G1 X220 Y220\n\
; OBJECT_ID: 1\n\
G1 X100 Y100 F30000\n\
G1 Z.2\n\
G1 E.8 F1800\n\
; FEATURE: Outer wall\n\
G1 X101 Y100 E.1\n";
        let result = strip_marked_blocks(gcode, &[(4, 4)]).unwrap();
        let text = String::from_utf8(result.output).unwrap();
        assert!(text.contains("G1 E.8 F1800"));
        assert_eq!(result.orphan_unretractions_removed, 0);
    }
}
