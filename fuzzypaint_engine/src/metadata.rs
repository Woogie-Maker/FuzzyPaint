//! Port of the metadata/progress-time rewriting in `gcode_single_filament.py`:
//! `_parse_time_text`, `_format_time`, `_estimate_progress_times`,
//! `_rewrite_gcode_metadata`, `_rewrite_slice_info`, `_rewrite_model_settings`,
//! `_rewrite_plate_json`, `_rewrite_sequence`, `_rewrite_project_settings`.

use std::collections::HashMap;

use crate::gcode::{is_ascii_ws, scan_fields, split_lines_with_endings, strip};
use crate::xmltree;

/// Python's `round(x)` for a float: round-half-to-even, not Rust's
/// round-half-away-from-zero. An exact tie is astronomically unlikely for
/// the computed time values this is used on, but implemented properly
/// rather than assumed close enough.
fn python_round(x: f64) -> i64 {
    let floor = x.floor();
    let diff = x - floor;
    let floor_i = floor as i64;
    if diff < 0.5 {
        floor_i
    } else if diff > 0.5 {
        floor_i + 1
    } else if floor_i % 2 == 0 {
        floor_i
    } else {
        floor_i + 1
    }
}

fn find_digits_before_char(value: &[u8], marker: u8) -> Option<i64> {
    let mut i = 0usize;
    while i < value.len() {
        if value[i].is_ascii_digit() {
            let start = i;
            while i < value.len() && value[i].is_ascii_digit() {
                i += 1;
            }
            if i < value.len() && value[i] == marker {
                return std::str::from_utf8(&value[start..i]).ok()?.parse().ok();
            }
            continue;
        }
        i += 1;
    }
    None
}

/// Port of `_parse_time_text`.
pub fn parse_time_text(value: &[u8]) -> i64 {
    find_digits_before_char(value, b'h').unwrap_or(0) * 3600
        + find_digits_before_char(value, b'm').unwrap_or(0) * 60
        + find_digits_before_char(value, b's').unwrap_or(0)
}

/// Port of `_format_time`.
pub fn format_time(seconds: f64) -> String {
    let value = python_round(seconds).max(0);
    let hours = value / 3600;
    let remainder = value % 3600;
    let minutes = remainder / 60;
    let secs = remainder % 60;
    if hours != 0 {
        format!("{hours}h {minutes}m {secs}s")
    } else {
        format!("{minutes}m {secs}s")
    }
}

fn find_subslice_ci(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    if needle.is_empty() || haystack.len() < needle.len() {
        return None;
    }
    (0..=haystack.len() - needle.len()).find(|&start| {
        haystack[start..start + needle.len()]
            .iter()
            .zip(needle)
            .all(|(a, b)| a.to_ascii_lowercase() == b.to_ascii_lowercase())
    })
}

fn is_m73_l1(stripped: &[u8]) -> bool {
    if !stripped.starts_with(b"M73") {
        return false;
    }
    let mut i = 3;
    let ws_start = i;
    while i < stripped.len() && is_ascii_ws(stripped[i]) {
        i += 1;
    }
    if i == ws_start {
        return false;
    }
    stripped[i..].starts_with(b"L1") && (stripped.len() == i + 2 || is_ascii_ws(stripped[i + 2]))
}

fn is_m73_p(stripped: &[u8]) -> bool {
    if !stripped.starts_with(b"M73") {
        return false;
    }
    let mut i = 3;
    let ws_start = i;
    while i < stripped.len() && is_ascii_ws(stripped[i]) {
        i += 1;
    }
    if i == ws_start || stripped.get(i) != Some(&b'P') {
        return false;
    }
    let digits_start = i + 1;
    let mut j = digits_start;
    while j < stripped.len() && stripped[j].is_ascii_digit() {
        j += 1;
    }
    j > digits_start
}

/// Matches `^M204\s+S([0-9.]+)` -- note the character class is literally
/// "digits or dots", not the usual float grammar.
fn match_m204_s(stripped: &[u8]) -> Option<f64> {
    if !stripped.starts_with(b"M204") {
        return None;
    }
    let mut i = 4;
    let ws_start = i;
    while i < stripped.len() && is_ascii_ws(stripped[i]) {
        i += 1;
    }
    if i == ws_start || stripped.get(i) != Some(&b'S') {
        return None;
    }
    i += 1;
    let digits_start = i;
    while i < stripped.len() && (stripped[i].is_ascii_digit() || stripped[i] == b'.') {
        i += 1;
    }
    if i == digits_start {
        return None;
    }
    std::str::from_utf8(&stripped[digits_start..i]).ok()?.parse().ok()
}

fn xyzeijfsp_fields(stripped: &[u8]) -> HashMap<u8, f64> {
    let mut fields = HashMap::new();
    for (letter, value) in scan_fields(stripped, b"XYZEIJFSP") {
        fields.insert(letter, value);
    }
    fields
}

/// Port of `_estimate_progress_times`. Returns `(model_seconds,
/// preparation_seconds, remaining_by_line)`.
pub fn estimate_progress_times(gcode: &[u8]) -> (f64, f64, HashMap<usize, f64>) {
    let mut x = 0.0f64;
    let mut y = 0.0f64;
    let mut z = 0.0f64;
    let mut e = 0.0f64;
    let mut feed = 3000.0f64;
    let mut acceleration = 500.0f64;
    let mut relative_xyz = false;
    let mut relative_e = true;
    let mut started = false;
    let mut naive = 0.0f64;
    let mut stopped = 0.0f64;
    let mut checkpoints: HashMap<usize, (f64, f64)> = HashMap::new();
    let mut preparation_seconds = 0.0f64;

    for (index, &raw) in split_lines_with_endings(gcode).iter().enumerate() {
        let line_number = index + 1;
        let stripped = strip(raw);

        if stripped.starts_with(b"; model printing time:") {
            let after_prefix = &stripped[b"; model printing time:".len()..];
            if let Some(pos) = find_subslice_ci(after_prefix, b"total estimated time:") {
                let group1 = trim_trailing_semicolon_ws(&after_prefix[..pos]);
                let group2 = strip_leading_ws(&after_prefix[pos + b"total estimated time:".len()..]);
                preparation_seconds =
                    (parse_time_text(group2) - parse_time_text(group1)).max(0) as f64;
            }
        }
        if is_m73_l1(stripped) {
            started = true;
        }
        if is_m73_p(stripped) {
            checkpoints.insert(line_number, (naive, stopped));
        }
        if stripped == b"G90" {
            relative_xyz = false;
            continue;
        }
        if stripped == b"G91" {
            relative_xyz = true;
            continue;
        }
        if stripped == b"M82" {
            relative_e = false;
            continue;
        }
        if stripped == b"M83" {
            relative_e = true;
            continue;
        }
        if let Some(value) = match_m204_s(stripped) {
            acceleration = value.max(1.0);
            continue;
        }

        let command = crate::gcode::first_token_upper(stripped);
        let fields = xyzeijfsp_fields(stripped);
        if let Some(&f) = fields.get(&b'F') {
            feed = f;
        }
        if command == b"G92" {
            x = fields.get(&b'X').copied().unwrap_or(x);
            y = fields.get(&b'Y').copied().unwrap_or(y);
            z = fields.get(&b'Z').copied().unwrap_or(z);
            e = fields.get(&b'E').copied().unwrap_or(e);
            continue;
        }
        if command == b"G4" && started {
            let dwell = fields.get(&b'S').copied().unwrap_or(0.0) + fields.get(&b'P').copied().unwrap_or(0.0) / 1000.0;
            naive += dwell;
            stopped += dwell;
            continue;
        }
        if !matches!(command.as_slice(), b"G0" | b"G1" | b"G2" | b"G3") {
            continue;
        }

        let nx = if relative_xyz { x + fields.get(&b'X').copied().unwrap_or(0.0) } else { fields.get(&b'X').copied().unwrap_or(x) };
        let ny = if relative_xyz { y + fields.get(&b'Y').copied().unwrap_or(0.0) } else { fields.get(&b'Y').copied().unwrap_or(y) };
        let nz = if relative_xyz { z + fields.get(&b'Z').copied().unwrap_or(0.0) } else { fields.get(&b'Z').copied().unwrap_or(z) };
        let ne = if relative_e { e + fields.get(&b'E').copied().unwrap_or(0.0) } else { fields.get(&b'E').copied().unwrap_or(e) };

        let mut distance = if matches!(command.as_slice(), b"G2" | b"G3") && (fields.contains_key(&b'I') || fields.contains_key(&b'J')) {
            let cx = x + fields.get(&b'I').copied().unwrap_or(0.0);
            let cy = y + fields.get(&b'J').copied().unwrap_or(0.0);
            let radius = (x - cx).hypot(y - cy);
            let first_angle = (y - cy).atan2(x - cx);
            let last_angle = (ny - cy).atan2(nx - cx);
            let two_pi = 2.0 * std::f64::consts::PI;
            let sweep = if command == b"G3" {
                (last_angle - first_angle).rem_euclid(two_pi)
            } else {
                (first_angle - last_angle).rem_euclid(two_pi)
            };
            (radius * sweep).abs()
        } else {
            ((nx - x).powi(2) + (ny - y).powi(2) + (nz - z).powi(2)).sqrt()
        };
        if distance < 1e-9 && fields.contains_key(&b'E') {
            distance = (ne - e).abs();
        }
        if started && distance > 0.0 {
            let speed = (feed / 60.0).max(1e-6);
            naive += distance / speed;
            let critical = speed * speed / acceleration;
            stopped += if distance < critical {
                2.0 * (distance / acceleration).sqrt()
            } else {
                2.0 * speed / acceleration + (distance - critical) / speed
            };
        }
        x = nx;
        y = ny;
        z = nz;
        e = ne;
    }

    let blend = 0.76f64;
    let model_seconds = naive + blend * (stopped - naive);
    let remaining: HashMap<usize, f64> = checkpoints
        .into_iter()
        .map(|(line, (n, s))| (line, (model_seconds - (n + blend * (s - n))).max(0.0)))
        .collect();
    (model_seconds, preparation_seconds, remaining)
}

fn trim_trailing_semicolon_ws(bytes: &[u8]) -> &[u8] {
    let mut end = bytes.len();
    while end > 0 && is_ascii_ws(bytes[end - 1]) {
        end -= 1;
    }
    if end > 0 && bytes[end - 1] == b';' {
        end -= 1;
    }
    while end > 0 && is_ascii_ws(bytes[end - 1]) {
        end -= 1;
    }
    &bytes[..end]
}

fn strip_leading_ws(bytes: &[u8]) -> &[u8] {
    let start = bytes.iter().position(|&b| !is_ascii_ws(b)).unwrap_or(bytes.len());
    &bytes[start..]
}

fn split_ending(line: &[u8]) -> (&[u8], &[u8]) {
    if line.ends_with(b"\r\n") {
        (&line[..line.len() - 2], &line[line.len() - 2..])
    } else if line.ends_with(b"\n") {
        (&line[..line.len() - 1], &line[line.len() - 1..])
    } else {
        (line, &line[line.len()..])
    }
}

/// Mirrors `^(\s*)(M62[01])(\s+)S(\d+)A(.*)$`, case-insensitive, returning
/// the rewritten content (S-value forced to `0`) when it matches and the
/// S-value is `< 255`.
fn rewrite_ams_select(content: &[u8]) -> Option<Vec<u8>> {
    let mut i = 0usize;
    while i < content.len() && is_ascii_ws(content[i]) {
        i += 1;
    }
    let leading_ws = &content[..i];
    if content.get(i).map(|b| b.to_ascii_uppercase()) != Some(b'M') {
        return None;
    }
    if content.get(i + 1) != Some(&b'6') || content.get(i + 2) != Some(&b'2') {
        return None;
    }
    let digit = *content.get(i + 3)?;
    if digit != b'0' && digit != b'1' {
        return None;
    }
    let command = &content[i..i + 4];
    i += 4;
    let ws2_start = i;
    while i < content.len() && is_ascii_ws(content[i]) {
        i += 1;
    }
    if i == ws2_start {
        return None;
    }
    let ws2 = &content[ws2_start..i];
    if content.get(i).map(|b| b.to_ascii_uppercase()) != Some(b'S') {
        return None;
    }
    i += 1;
    let digits_start = i;
    while i < content.len() && content[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return None;
    }
    let s_value: i64 = std::str::from_utf8(&content[digits_start..i]).ok()?.parse().ok()?;
    if content.get(i).map(|b| b.to_ascii_uppercase()) != Some(b'A') {
        return None;
    }
    if s_value >= 255 {
        return None;
    }
    let mut result = Vec::new();
    result.extend_from_slice(leading_ws);
    result.extend_from_slice(command);
    result.extend_from_slice(ws2);
    result.extend_from_slice(b"S0A");
    result.extend_from_slice(&content[i + 1..]);
    Some(result)
}

/// Mirrors `^(\s*)T(\d+)(\s*(?:;.*)?)$`, case-insensitive.
fn rewrite_tool_select(content: &[u8]) -> Option<Vec<u8>> {
    let mut i = 0usize;
    while i < content.len() && is_ascii_ws(content[i]) {
        i += 1;
    }
    let leading_ws = &content[..i];
    if content.get(i).map(|b| b.to_ascii_uppercase()) != Some(b'T') {
        return None;
    }
    i += 1;
    let digits_start = i;
    while i < content.len() && content[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return None;
    }
    let value: i64 = std::str::from_utf8(&content[digits_start..i]).ok()?.parse().ok()?;
    let group3_start = i;
    let mut j = i;
    while j < content.len() && is_ascii_ws(content[j]) {
        j += 1;
    }
    if j != content.len() && content[j] != b';' {
        return None;
    }
    if value >= 255 {
        return None;
    }
    let mut result = Vec::new();
    result.extend_from_slice(leading_ws);
    result.extend_from_slice(b"T0");
    result.extend_from_slice(&content[group3_start..]);
    Some(result)
}

fn match_m73_layer(content: &[u8]) -> Option<i64> {
    if !content.starts_with(b"M73") {
        return None;
    }
    let mut i = 3;
    let ws_start = i;
    while i < content.len() && is_ascii_ws(content[i]) {
        i += 1;
    }
    if i == ws_start || content.get(i) != Some(&b'L') {
        return None;
    }
    i += 1;
    let digits_start = i;
    while i < content.len() && content[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return None;
    }
    if i < content.len() && !is_ascii_ws(content[i]) {
        return None;
    }
    std::str::from_utf8(&content[digits_start..i]).ok()?.parse().ok()
}

/// Mirrors `^M73\s+P\d+(?:\s+R-?\d+)?(?P<tail>\s*;.*)?$`, returning the
/// `tail` group's bytes (empty if the line ends with no trailing comment).
fn match_m73_progress(content: &[u8]) -> Option<Vec<u8>> {
    if !content.starts_with(b"M73") {
        return None;
    }
    let mut i = 3;
    let ws_start = i;
    while i < content.len() && is_ascii_ws(content[i]) {
        i += 1;
    }
    if i == ws_start || content.get(i) != Some(&b'P') {
        return None;
    }
    i += 1;
    let digits_start = i;
    while i < content.len() && content[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return None;
    }
    let mut j = i;
    while j < content.len() && is_ascii_ws(content[j]) {
        j += 1;
    }
    if j > i && content.get(j) == Some(&b'R') {
        let mut k = j + 1;
        if content.get(k) == Some(&b'-') {
            k += 1;
        }
        let rdigits_start = k;
        while k < content.len() && content[k].is_ascii_digit() {
            k += 1;
        }
        if k > rdigits_start {
            i = k;
        }
    }
    let tail_start = i;
    let mut j = i;
    while j < content.len() && is_ascii_ws(content[j]) {
        j += 1;
    }
    if j < content.len() && content[j] == b';' {
        Some(content[tail_start..].to_vec())
    } else if i == content.len() {
        Some(Vec::new())
    } else {
        None
    }
}

pub struct GcodeMetadataResult {
    pub output: Vec<u8>,
    pub md5_hex: String,
    pub filament_volume_mm3: f64,
    pub filament_weight_g: f64,
    pub estimated_model_seconds: f64,
    pub estimated_total_seconds: f64,
}

/// Port of `_rewrite_gcode_metadata`.
pub fn rewrite_gcode_metadata(gcode: &[u8], filament_length_mm: f64) -> GcodeMetadataResult {
    use md5::{Digest, Md5};

    let diameter = 1.75f64;
    let density = 1.32f64;
    let filament_volume_mm3 = filament_length_mm * std::f64::consts::PI * (diameter / 2.0).powi(2);
    let filament_weight_g = filament_volume_mm3 / 1000.0 * density;

    let fixed_replacements: [(&[u8], String); 5] = [
        (b"; total filament length [mm] :", format!("; total filament length [mm] : {filament_length_mm:.2}")),
        (b"; total filament volume [cm^3] :", format!("; total filament volume [cm^3] : {filament_volume_mm3:.2}")),
        (b"; total filament weight [g] :", format!("; total filament weight [g] : {filament_weight_g:.2}")),
        (b"; filament:", "; filament: 1".to_string()),
        (b"; enable_prime_tower =", "; enable_prime_tower = 0".to_string()),
    ];
    let single_value_metadata: [(&[u8], u8); 7] = [
        (b"; filament_map =", b','),
        (b"; filament_nozzle_map =", b','),
        (b"; filament_colour =", b';'),
        (b"; filament_colour_type =", b';'),
        (b"; filament_type =", b';'),
        (b"; filament_settings_id =", b';'),
        (b"; filament_ids =", b';'),
    ];

    let (model_seconds, preparation_seconds, remaining_by_line) = estimate_progress_times(gcode);
    let estimated_total_seconds = model_seconds + preparation_seconds;

    let mut total_layers = 0i64;
    for &raw in &split_lines_with_endings(gcode) {
        if let Some(value) = match_m73_layer(strip(raw)) {
            total_layers = total_layers.max(value);
        }
    }

    let mut md5 = Md5::new();
    let mut current_layer = 0i64;
    let mut output: Vec<u8> = Vec::with_capacity(gcode.len());

    for (index, &raw) in split_lines_with_endings(gcode).iter().enumerate() {
        let line_number = index + 1;
        let (content_slice, ending) = split_ending(raw);
        let mut content: Vec<u8> = content_slice.to_vec();

        if let Some(rewritten) = rewrite_ams_select(&content) {
            content = rewritten;
        }
        if let Some(rewritten) = rewrite_tool_select(&content) {
            content = rewritten;
        }

        if let Some(layer) = match_m73_layer(&content) {
            current_layer = layer;
        }
        if let Some(tail) = match_m73_progress(&content) {
            if total_layers > 0 {
                let percent = 0i64.max(100.min(python_round(current_layer as f64 * 100.0 / total_layers as f64)));
                let mut remaining = remaining_by_line.get(&line_number).copied().unwrap_or(0.0);
                if current_layer == 0 {
                    remaining += preparation_seconds;
                }
                let minutes = if percent >= 100 { 0 } else { 1i64.max((remaining / 60.0).ceil() as i64) };
                let mut rewritten = format!("M73 P{percent} R{minutes}").into_bytes();
                rewritten.extend_from_slice(&tail);
                content = rewritten;
            }
        }

        for (prefix, replacement) in &fixed_replacements {
            if content.starts_with(prefix) {
                content = replacement.clone().into_bytes();
                break;
            }
        }
        for (prefix, delimiter) in &single_value_metadata {
            if content.starts_with(prefix) {
                if let Some(eq_pos) = content.iter().position(|&b| b == b'=') {
                    let after_eq = strip_both(&content[eq_pos + 1..]);
                    let first = match after_eq.iter().position(|&b| b == *delimiter) {
                        Some(delim_pos) => strip_both(&after_eq[..delim_pos]),
                        None => after_eq,
                    };
                    let mut rewritten = prefix.to_vec();
                    rewritten.push(b' ');
                    rewritten.extend_from_slice(first);
                    content = rewritten;
                }
                break;
            }
        }
        if content.starts_with(b"; model printing time:") {
            content = format!(
                "; model printing time: {}; total estimated time: {}",
                format_time(model_seconds),
                format_time(model_seconds + preparation_seconds)
            )
            .into_bytes();
        }

        let mut written = content;
        written.extend_from_slice(ending);
        output.extend_from_slice(&written);
        md5.update(&written);
    }

    GcodeMetadataResult {
        output,
        md5_hex: hex_encode(&md5.finalize()),
        filament_volume_mm3,
        filament_weight_g,
        estimated_model_seconds: model_seconds,
        estimated_total_seconds,
    }
}

fn strip_both(bytes: &[u8]) -> &[u8] {
    strip(bytes)
}

fn hex_encode(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

// ---------------------------------------------------------------------------
// XML rewriters
// ---------------------------------------------------------------------------

/// Port of `_rewrite_slice_info`.
pub fn rewrite_slice_info(
    data: &[u8],
    filament_weight_g: f64,
    filament_length_mm: f64,
    estimated_total_seconds: f64,
) -> Result<Vec<u8>, String> {
    let mut root = xmltree::parse(data)?;
    let plate = root.find_child_mut("plate").ok_or("slice_info.config contains no plate")?;
    for metadata in plate.find_children_mut("metadata") {
        match metadata.get("key") {
            Some("weight") => metadata.set("value", format!("{filament_weight_g:.2}")),
            Some("prediction") => metadata.set("value", python_round(estimated_total_seconds).to_string()),
            Some("filament_maps") => metadata.set("value", "1"),
            Some("limit_filament_maps") => metadata.set("value", "0"),
            _ => {}
        }
    }
    let has_filament = plate.find_children("filament").next().is_some();
    if !has_filament {
        return Err("slice_info.config contains no filament".to_string());
    }
    {
        let first = plate.find_children_mut("filament").next().unwrap();
        first.set("id", "1");
        first.set("used_m", format!("{:.2}", filament_length_mm / 1000.0));
        first.set("used_g", format!("{filament_weight_g:.2}"));
    }
    plate.keep_first_children("filament", 1);
    if let Some(lists) = plate.find_child_mut("layer_filament_lists") {
        for item in lists.find_children_mut("layer_filament_list") {
            item.set("filament_list", "0");
        }
    }
    let mut out = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n".to_vec();
    out.extend(xmltree::serialize_indented(&root));
    out.push(b'\n');
    Ok(out)
}

/// Port of `_rewrite_model_settings`.
pub fn rewrite_model_settings(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut root = xmltree::parse(data).map_err(|_| "Could not read model_settings.config".to_string())?;
    if let Some(plate) = root.find_child_mut("plate") {
        for metadata in plate.find_children_mut("metadata") {
            match metadata.get("key") {
                Some("filament_maps") => metadata.set("value", "1"),
                Some("filament_volume_maps") => metadata.set("value", "0"),
                _ => {}
            }
        }
    }
    let mut out = b"<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n".to_vec();
    out.extend(xmltree::serialize_indented(&root));
    out.push(b'\n');
    Ok(out)
}

// ---------------------------------------------------------------------------
// JSON rewriters
// ---------------------------------------------------------------------------

fn strip_bom(data: &[u8]) -> &[u8] {
    data.strip_prefix(&[0xEFu8, 0xBB, 0xBF]).unwrap_or(data)
}

fn to_compact_json(value: &serde_json::Value) -> String {
    let raw = serde_json::to_string(value).expect("serializing a parsed JSON value cannot fail");
    normalize_json_numbers(&raw)
}

/// `serde_json` is built with `arbitrary_precision` so pass-through numbers
/// keep their exact original decimal text instead of serde_json's own
/// (occasionally imprecise) default float parsing -- but that means a
/// pass-through float is emitted VERBATIM, whereas Python's `json.loads`
/// always parses to an `f64` and `json.dumps` always re-emits via that
/// float's shortest round-trip decimal text, which can differ from the
/// original source text. This walks the serialized JSON text (tracking
/// whether we're inside a string literal, since JSON structure/numbers are
/// always ASCII and only ever appear outside strings) and re-renders every
/// number literal through an f64 round-trip, to match that Python behavior
/// bit-for-bit in the overwhelming majority of cases.
///
/// One residual, accepted gap: when a decimal literal sits so close to the
/// midpoint between two representable doubles that BOTH are equally valid
/// "shortest round-trip" results, Python's and Rust's shortest-float
/// algorithms can make different (but equally correct) tie-breaking
/// choices. Same category as the G2/G3 arc sin/cos/atan2 ULP finding in
/// step 4/5b -- not a bug, not chased further; these are cosmetic
/// preview/UI metadata fields with no print-physical consequence.
fn normalize_json_numbers(text: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let mut out = String::with_capacity(text.len());
    let mut i = 0usize;
    let mut in_string = false;
    let mut escape_next = false;
    while i < chars.len() {
        let c = chars[i];
        if in_string {
            out.push(c);
            if escape_next {
                escape_next = false;
            } else if c == '\\' {
                escape_next = true;
            } else if c == '"' {
                in_string = false;
            }
            i += 1;
            continue;
        }
        if c == '"' {
            in_string = true;
            out.push(c);
            i += 1;
            continue;
        }
        if c == '-' || c.is_ascii_digit() {
            let start = i;
            if c == '-' {
                i += 1;
            }
            while i < chars.len() && chars[i].is_ascii_digit() {
                i += 1;
            }
            let mut is_float = false;
            if i < chars.len() && chars[i] == '.' {
                is_float = true;
                i += 1;
                while i < chars.len() && chars[i].is_ascii_digit() {
                    i += 1;
                }
            }
            if i < chars.len() && (chars[i] == 'e' || chars[i] == 'E') {
                is_float = true;
                i += 1;
                if i < chars.len() && (chars[i] == '+' || chars[i] == '-') {
                    i += 1;
                }
                while i < chars.len() && chars[i].is_ascii_digit() {
                    i += 1;
                }
            }
            let token: String = chars[start..i].iter().collect();
            if is_float {
                match token.parse::<f64>() {
                    Ok(value) => out.push_str(&format_float_like_python(value)),
                    Err(_) => out.push_str(&token),
                }
            } else {
                out.push_str(&token);
            }
            continue;
        }
        out.push(c);
        i += 1;
    }
    out
}

/// Mirrors Python's `repr(float)` (what `json.dumps` uses for a float):
/// the shortest round-trip decimal text, always containing a `.` (unlike
/// Rust's `Display`, which drops it for whole numbers), switching to
/// scientific notation outside `[1e-4, 1e16)`.
fn format_float_like_python(value: f64) -> String {
    if !value.is_finite() {
        return value.to_string();
    }
    let abs = value.abs();
    if abs != 0.0 && (abs < 1e-4 || abs >= 1e16) {
        return format_scientific_like_python(value);
    }
    let text = value.to_string();
    if text.contains('.') {
        text
    } else {
        format!("{text}.0")
    }
}

fn format_scientific_like_python(value: f64) -> String {
    let formatted = format!("{value:e}");
    let (mantissa, exponent_str) = formatted.split_once('e').unwrap();
    let exponent: i32 = exponent_str.parse().unwrap();
    format!("{mantissa}e{}{:02}", if exponent >= 0 { "+" } else { "-" }, exponent.abs())
}

/// Mirrors `json.dumps(..., separators=(",", ":"))` WITHOUT `ensure_ascii=False`
/// -- Python's default escapes every non-ASCII character as `\uXXXX`.
fn ascii_escape_json(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for ch in s.chars() {
        if ch.is_ascii() {
            out.push(ch);
        } else {
            let mut buf = [0u16; 2];
            for unit in ch.encode_utf16(&mut buf) {
                out.push_str(&format!("\\u{unit:04x}"));
            }
        }
    }
    out
}

/// Port of `_rewrite_plate_json`.
pub fn rewrite_plate_json(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut value: serde_json::Value =
        serde_json::from_slice(strip_bom(data)).map_err(|e| format!("invalid plate_1.json: {e}"))?;
    let obj = value.as_object_mut().ok_or("plate_1.json is not a JSON object")?;
    let colors = obj.get("filament_colors").and_then(|v| v.as_array()).cloned();
    let new_colors = match colors {
        Some(colors) if !colors.is_empty() => vec![colors[0].clone()],
        _ => vec![serde_json::Value::String("#757575".to_string())],
    };
    obj.insert("filament_colors".to_string(), serde_json::Value::Array(new_colors));
    obj.insert("filament_ids".to_string(), serde_json::json!([0]));
    obj.insert("first_extruder".to_string(), serde_json::json!(0));
    if let Some(objects) = obj.get("bbox_objects").and_then(|v| v.as_array()).cloned() {
        let filtered: Vec<serde_json::Value> = objects
            .into_iter()
            .filter(|item| item.get("name").and_then(|n| n.as_str()) != Some("wipe_tower"))
            .collect();
        obj.insert("bbox_objects".to_string(), serde_json::Value::Array(filtered));
    }
    Ok(ascii_escape_json(&to_compact_json(&value)).into_bytes())
}

/// Port of `_rewrite_sequence`.
pub fn rewrite_sequence(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut value: serde_json::Value =
        serde_json::from_slice(strip_bom(data)).map_err(|e| format!("invalid filament_sequence.json: {e}"))?;
    let obj = value.as_object_mut().ok_or("filament_sequence.json is not a JSON object")?;
    for (_, plate) in obj.iter_mut() {
        let Some(plate) = plate.as_object_mut() else { continue };
        if let Some(sequence) = plate.get("sequence").and_then(|v| v.as_array()).map(|a| a.len()) {
            plate.insert("sequence".to_string(), serde_json::Value::Array(vec![serde_json::json!(1); sequence]));
        }
        if let Some(nozzle) = plate.get("nozzle_sequence").and_then(|v| v.as_array()).map(|a| a.len()) {
            plate.insert("nozzle_sequence".to_string(), serde_json::Value::Array(vec![serde_json::json!(0); nozzle]));
        }
        if plate.get("optimal_assignment").and_then(|v| v.as_array()).is_some() {
            plate.insert("optimal_assignment".to_string(), serde_json::json!([0]));
        }
    }
    Ok(ascii_escape_json(&to_compact_json(&value)).into_bytes())
}

/// Port of `_rewrite_project_settings`.
pub fn rewrite_project_settings(data: &[u8]) -> Result<Vec<u8>, String> {
    let mut value: serde_json::Value = serde_json::from_slice(strip_bom(data))
        .map_err(|_| "Could not read project_settings.config".to_string())?;
    let obj = value.as_object_mut().ok_or("project_settings.config is not a JSON object")?;

    let filament_count = obj.get("filament_colour").and_then(|v| v.as_array()).map(|a| a.len()).unwrap_or(0);
    if filament_count > 1 {
        let keys: Vec<String> = obj.keys().cloned().collect();
        for key in keys {
            let Some(array) = obj.get(&key).and_then(|v| v.as_array()).cloned() else { continue };
            if array.is_empty() {
                continue;
            }
            let len = array.len();
            let new_value = if (key == "different_settings_to_system" || key == "inherits_group")
                && len == filament_count + 2
            {
                Some(vec![array[0].clone(), array[1].clone(), array[len - 1].clone()])
            } else if key == "flush_volumes_matrix" && len == filament_count * filament_count {
                Some(vec![array[0].clone()])
            } else if len == filament_count {
                Some(vec![array[0].clone()])
            } else if len == filament_count * 2 {
                Some(array[..2].to_vec())
            } else if len == filament_count * 4 {
                Some(array[..4].to_vec())
            } else {
                None
            };
            if let Some(new_value) = new_value {
                obj.insert(key, serde_json::Value::Array(new_value));
            }
        }
    }
    obj.insert("enable_prime_tower".to_string(), serde_json::Value::String("0".to_string()));
    if let Some(minimal) = obj.get("filament_minimal_purge_on_wipe_tower").and_then(|v| v.as_array()).map(|a| a.len())
    {
        obj.insert(
            "filament_minimal_purge_on_wipe_tower".to_string(),
            serde_json::Value::Array(vec![serde_json::Value::String("0".to_string()); minimal]),
        );
    }
    Ok(to_compact_json(&value).into_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_time_text() {
        assert_eq!(parse_time_text(b"1h 2m 3s"), 3723);
        assert_eq!(parse_time_text(b"5m 10s"), 310);
        assert_eq!(parse_time_text(b"42s"), 42);
        assert_eq!(parse_time_text(b""), 0);
    }

    #[test]
    fn formats_time() {
        assert_eq!(format_time(3723.0), "1h 2m 3s");
        assert_eq!(format_time(310.0), "5m 10s");
        assert_eq!(format_time(0.0), "0m 0s");
        assert_eq!(format_time(-5.0), "0m 0s");
    }

    #[test]
    fn python_round_ties_to_even() {
        assert_eq!(python_round(2.5), 2);
        assert_eq!(python_round(3.5), 4);
        assert_eq!(python_round(2.4), 2);
        assert_eq!(python_round(2.6), 3);
    }

    #[test]
    fn rewrites_ams_select() {
        assert_eq!(rewrite_ams_select(b"M620 S5A1").unwrap(), b"M620 S0A1".to_vec());
        assert_eq!(rewrite_ams_select(b"m621 S254A"), Some(b"m621 S0A".to_vec()));
        assert_eq!(rewrite_ams_select(b"M620 S255A1"), None);
        assert_eq!(rewrite_ams_select(b"G1 X1"), None);
    }

    #[test]
    fn rewrites_tool_select() {
        assert_eq!(rewrite_tool_select(b"T1"), Some(b"T0".to_vec()));
        assert_eq!(rewrite_tool_select(b"T1 ; comment"), Some(b"T0 ; comment".to_vec()));
        assert_eq!(rewrite_tool_select(b"T255"), None);
        assert_eq!(rewrite_tool_select(b"T1 junk"), None);
    }

    #[test]
    fn matches_m73_progress_tail() {
        assert_eq!(match_m73_progress(b"M73 P50"), Some(Vec::new()));
        assert_eq!(match_m73_progress(b"M73 P50 R10"), Some(Vec::new()));
        assert_eq!(match_m73_progress(b"M73 P50 R10 ; note"), Some(b" ; note".to_vec()));
        assert_eq!(match_m73_progress(b"M73 P50 junk"), None);
    }
}

