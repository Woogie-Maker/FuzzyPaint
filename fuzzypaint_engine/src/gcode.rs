//! Byte-level G-code line/field parsing shared by every stage of the engine.
//!
//! These are hand-written equivalents of the regexes the Python implementation
//! uses (`FIELD_RE`, `XYE_FIELD_RE`, `E_RE`, `MOVE_RE`, ...). They are written
//! to match Python's `re` semantics byte-for-byte (including its quirks, e.g.
//! `\s` meaning ASCII whitespace only in a `bytes` pattern) so that porting a
//! function here can be verified against the original with differential tests.

/// Python's `\s` in a `bytes` regex pattern (no `re.UNICODE`): ASCII whitespace.
pub fn is_ascii_ws(b: u8) -> bool {
    matches!(b, b' ' | b'\t' | b'\n' | b'\r' | 0x0b | 0x0c)
}

/// Equivalent of Python's `bytes.strip()` (default: strip ASCII whitespace).
pub fn strip(bytes: &[u8]) -> &[u8] {
    let start = bytes.iter().position(|&b| !is_ascii_ws(b));
    let Some(start) = start else { return &bytes[0..0] };
    let end = bytes.iter().rposition(|&b| !is_ascii_ws(b)).map(|i| i + 1).unwrap_or(start);
    &bytes[start..end]
}

/// Splits into lines the way Python iterates a binary file object: split on
/// `\n`, each yielded line keeps its own trailing `\n` (and any `\r` before
/// it) if present, and a buffer ending in `\n` produces no trailing empty
/// element. An empty buffer yields zero lines.
pub fn split_lines_with_endings(gcode: &[u8]) -> Vec<&[u8]> {
    let mut result = Vec::new();
    let mut start = 0usize;
    for i in 0..gcode.len() {
        if gcode[i] == b'\n' {
            result.push(&gcode[start..=i]);
            start = i + 1;
        }
    }
    if start < gcode.len() {
        result.push(&gcode[start..]);
    }
    result
}

/// Parses a float literal matching the regex grammar `-?(?:\d+(?:\.\d*)?|\.\d+)`
/// starting at `start`. Returns `(value, end_index)` on success, mirroring how
/// far a regex engine would consume.
pub fn parse_number(bytes: &[u8], start: usize) -> Option<(f64, usize)> {
    let mut i = start;
    if i < bytes.len() && bytes[i] == b'-' {
        i += 1;
    }
    let digits_start = i;
    while i < bytes.len() && bytes[i].is_ascii_digit() {
        i += 1;
    }
    let int_digits = i - digits_start;
    let end = if int_digits > 0 {
        let mut j = i;
        if j < bytes.len() && bytes[j] == b'.' {
            j += 1;
            while j < bytes.len() && bytes[j].is_ascii_digit() {
                j += 1;
            }
        }
        j
    } else if i < bytes.len() && bytes[i] == b'.' {
        let mut j = i + 1;
        while j < bytes.len() && bytes[j].is_ascii_digit() {
            j += 1;
        }
        if j > i + 1 {
            j
        } else {
            return None;
        }
    } else {
        return None;
    };
    let text = std::str::from_utf8(&bytes[start..end]).ok()?;
    let value: f64 = text.parse().ok()?;
    Some((value, end))
}

/// Same grammar as [`parse_number`] but without the optional leading `-`
/// (matches the unsigned-only regex alternative `(?:\d+(?:\.\d*)?|\.\d+)`
/// used by a couple of the original patterns).
fn parse_unsigned_number(bytes: &[u8], start: usize) -> Option<(f64, usize)> {
    let i = start;
    let digits_start = i;
    let mut i2 = i;
    while i2 < bytes.len() && bytes[i2].is_ascii_digit() {
        i2 += 1;
    }
    let int_digits = i2 - digits_start;
    let end = if int_digits > 0 {
        let mut j = i2;
        if j < bytes.len() && bytes[j] == b'.' {
            j += 1;
            while j < bytes.len() && bytes[j].is_ascii_digit() {
                j += 1;
            }
        }
        j
    } else if i2 < bytes.len() && bytes[i2] == b'.' {
        let mut j = i2 + 1;
        while j < bytes.len() && bytes[j].is_ascii_digit() {
            j += 1;
        }
        if j > i2 + 1 {
            j
        } else {
            return None;
        }
    } else {
        return None;
    };
    let text = std::str::from_utf8(&bytes[start..end]).ok()?;
    let value: f64 = text.parse().ok()?;
    Some((value, end))
}

/// Mirrors `re.findall(rb"(?:^|\s)([LETTERS])(-?(?:\d+(?:\.\d*)?|\.\d+))", line, re.IGNORECASE)`
/// for the given candidate `letters` (upper-case bytes), returning each match's
/// upper-cased letter and parsed value in order of appearance. On repeated
/// keys, the caller deciding "last wins" (as a Python dict comprehension would)
/// is left to the caller, matching how the original code consumes this.
pub fn scan_fields(line: &[u8], letters: &[u8]) -> Vec<(u8, f64)> {
    let mut out = Vec::new();
    let mut i = 0usize;
    let len = line.len();
    while i < len {
        let preceded_ok = i == 0 || is_ascii_ws(line[i - 1]);
        let mut advanced = false;
        if preceded_ok {
            let upper = line[i].to_ascii_uppercase();
            if letters.contains(&upper) {
                if let Some((value, end)) = parse_number(line, i + 1) {
                    out.push((upper, value));
                    i = end;
                    advanced = true;
                }
            }
        }
        if !advanced {
            i += 1;
        }
    }
    out
}

/// Mirrors `re.search(rb"(?:^|\s)E(-?(?:\d+(?:\.\d*)?|\.\d+))(?:\s|$)", line, re.IGNORECASE)`:
/// the first `E<number>` field that is also followed by whitespace or end-of-line.
pub fn find_e_value_bounded(line: &[u8]) -> Option<f64> {
    let len = line.len();
    let mut i = 0usize;
    while i < len {
        let preceded_ok = i == 0 || is_ascii_ws(line[i - 1]);
        if preceded_ok && line[i].to_ascii_uppercase() == b'E' {
            if let Some((value, end)) = parse_number(line, i + 1) {
                if end == len || is_ascii_ws(line[end]) {
                    return Some(value);
                }
            }
        }
        i += 1;
    }
    None
}

/// Mirrors `re.match(rb"^G[0123](?:\s|$)", line, re.IGNORECASE)`.
pub fn is_move_command(line: &[u8]) -> bool {
    if line.len() < 2 {
        return false;
    }
    if line[0].to_ascii_uppercase() != b'G' {
        return false;
    }
    if !matches!(line[1], b'0' | b'1' | b'2' | b'3') {
        return false;
    }
    line.len() == 2 || is_ascii_ws(line[2])
}

/// Mirrors `stripped.split(None, 1)[0].upper()` applied to an already-stripped
/// line (or `b""` if `line` is empty).
pub fn first_token_upper(line: &[u8]) -> Vec<u8> {
    let Some(start) = line.iter().position(|&b| !is_ascii_ws(b)) else {
        return Vec::new();
    };
    let end = line[start..]
        .iter()
        .position(|&b| is_ascii_ws(b))
        .map(|i| start + i)
        .unwrap_or(line.len());
    line[start..end].to_ascii_uppercase()
}

/// Mirrors the specific anchored pattern
/// `^G1\s+E(?P<value>(?:\d+(?:\.\d*)?|\.\d+))(?:\s+F\d+(?:\.\d*)?)?(?:\s*;.*)?$`
/// matched (not searched) against an already-stripped line, case-insensitively.
pub fn match_pure_e(stripped: &[u8]) -> Option<f64> {
    let len = stripped.len();
    if len < 2 || stripped[0].to_ascii_uppercase() != b'G' || stripped[1] != b'1' {
        return None;
    }
    let mut i = 2usize;
    let ws_start = i;
    while i < len && is_ascii_ws(stripped[i]) {
        i += 1;
    }
    if i == ws_start {
        return None; // \s+ requires at least one whitespace byte
    }
    if i >= len || stripped[i].to_ascii_uppercase() != b'E' {
        return None;
    }
    i += 1;
    let (value, end) = parse_unsigned_number(stripped, i)?;
    i = end;

    // Optional (?:\s+F\d+(?:\.\d*)?)? -- only consume it if it fully matches,
    // otherwise leave `i` where it was (regex backtracking).
    let before_feed = i;
    let mut j = i;
    while j < len && is_ascii_ws(stripped[j]) {
        j += 1;
    }
    if j > before_feed && j < len && stripped[j].to_ascii_uppercase() == b'F' {
        let digits_start = j + 1;
        let mut k = digits_start;
        while k < len && stripped[k].is_ascii_digit() {
            k += 1;
        }
        if k > digits_start {
            if k < len && stripped[k] == b'.' {
                k += 1;
                while k < len && stripped[k].is_ascii_digit() {
                    k += 1;
                }
            }
            i = k;
        }
    }

    // Optional (?:\s*;.*)? then required end-of-string.
    let before_comment = i;
    let mut j = i;
    while j < len && is_ascii_ws(stripped[j]) {
        j += 1;
    }
    if j < len && stripped[j] == b';' {
        return Some(value); // `.*` consumes the rest unconditionally
    }
    i = before_comment;
    if i == len {
        Some(value)
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_ascii_whitespace() {
        assert_eq!(strip(b"  G1 X1 \t\r\n"), b"G1 X1");
        assert_eq!(strip(b""), b"");
        assert_eq!(strip(b"   "), b"");
    }

    #[test]
    fn splits_lines_like_python_file_iteration() {
        assert_eq!(split_lines_with_endings(b"a\nb\n"), vec![&b"a\n"[..], &b"b\n"[..]]);
        assert_eq!(split_lines_with_endings(b"a\nb"), vec![&b"a\n"[..], &b"b"[..]]);
        assert_eq!(split_lines_with_endings(b""), Vec::<&[u8]>::new());
        assert_eq!(split_lines_with_endings(b"\n"), vec![&b"\n"[..]]);
    }

    #[test]
    fn parses_numbers() {
        assert_eq!(parse_number(b"100", 0), Some((100.0, 3)));
        assert_eq!(parse_number(b"-1.5abc", 0), Some((-1.5, 4)));
        assert_eq!(parse_number(b".5", 0), Some((0.5, 2)));
        assert_eq!(parse_number(b"-.5", 0), Some((-0.5, 3)));
        assert_eq!(parse_number(b"-", 0), None);
        assert_eq!(parse_number(b".", 0), None);
        assert_eq!(parse_number(b"abc", 0), None);
    }

    #[test]
    fn scans_xye_fields_in_order_last_wins_is_callers_job() {
        let fields = scan_fields(b"G1 X100 Y-100.5 E.8 F3000", b"XYE");
        assert_eq!(fields, vec![(b'X', 100.0), (b'Y', -100.5), (b'E', 0.8)]);
    }

    #[test]
    fn scans_fields_case_insensitively() {
        assert_eq!(scan_fields(b"g1 x1 y2", b"XY"), vec![(b'X', 1.0), (b'Y', 2.0)]);
    }

    #[test]
    fn finds_bounded_e_value() {
        assert_eq!(find_e_value_bounded(b"G1 E-2"), Some(-2.0));
        assert_eq!(find_e_value_bounded(b"G1 E.8 F1800"), Some(0.8));
        // Not bounded by whitespace/end -> no match (mirrors the trailing
        // `(?:\s|$)` requirement E_RE has but XYE_FIELD_RE does not).
        assert_eq!(find_e_value_bounded(b"G1 E1extra"), None);
    }

    #[test]
    fn recognizes_move_commands() {
        assert!(is_move_command(b"G1 X1 Y1"));
        assert!(is_move_command(b"G1"));
        assert!(is_move_command(b"g2 I1 J1"));
        assert!(!is_move_command(b"G10"));
        assert!(!is_move_command(b"G4"));
        assert!(!is_move_command(b""));
    }

    #[test]
    fn extracts_first_token_uppercased() {
        assert_eq!(first_token_upper(b"g1 x1 y1"), b"G1");
        assert_eq!(first_token_upper(b""), b"");
        assert_eq!(first_token_upper(b"T0"), b"T0");
    }

    #[test]
    fn matches_pure_e_lines() {
        assert_eq!(match_pure_e(b"G1 E0.8"), Some(0.8));
        assert_eq!(match_pure_e(b"G1 E0.8 F1800"), Some(0.8));
        assert_eq!(match_pure_e(b"G1 E0.8 F1800 ; comment"), Some(0.8));
        assert_eq!(match_pure_e(b"G1 E0.8 ; comment"), Some(0.8));
        assert_eq!(match_pure_e(b"g1 e.8"), Some(0.8));
        assert_eq!(match_pure_e(b"G1 X1 E0.8"), None); // X before E -> not pure
        assert_eq!(match_pure_e(b"G1 E-0.8"), None); // no sign allowed here
        assert_eq!(match_pure_e(b"G1 E0.8 trailing"), None); // junk with no ';'
    }
}
