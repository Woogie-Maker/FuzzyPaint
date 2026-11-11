//! Port of `collapse_gcode_3mf` from `gcode_single_filament.py`: the 3mf
//! zip orchestration that ties every earlier piece together -- read the
//! sliced package, find and strip the marked tool-change/tower blocks
//! (applying the detour `line_replacements`), rewrite the G-code metadata
//! and every companion XML/JSON file, and write the collapsed package.
//! `groove_lines`/`coordinate_overrides`/`extrusion_scales` are not
//! parameters here: confirmed unused by every real call site (see
//! `single_filament.rs`'s module doc comment).

use std::collections::HashMap;
use std::io::{Read, Write};

use crate::metadata;
use crate::single_filament;

#[derive(Default, Debug, Clone)]
pub struct CollapseReport {
    pub toolchanges_removed: u64,
    pub tower_blocks_removed: u64,
    pub kept_motion_lines: u64,
    pub filament_length_mm: f64,
    pub filament_volume_mm3: f64,
    pub filament_weight_g: f64,
    pub input_gcode_bytes: u64,
    pub output_gcode_bytes: u64,
    pub groove_segments: u64,
    pub attachments_removed: u64,
    pub attachment_bytes_removed: u64,
    pub tower_excursions_removed: u64,
    pub orphan_unretractions_removed: u64,
    pub estimated_model_seconds: f64,
    pub estimated_total_seconds: f64,
}

/// Mirrors `re.fullmatch(r"metadata/filament_settings_(\d+)\.config", lower)`
/// -- `lower` is already lowercased by the caller, so this only needs to
/// match that exact (lowercase) literal shape.
fn filament_settings_index(lower_name: &str) -> Option<i64> {
    let digits = lower_name.strip_prefix("metadata/filament_settings_")?.strip_suffix(".config")?;
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    digits.parse().ok()
}

/// Port of `collapse_gcode_3mf`. `source`/`destination` are filesystem
/// paths (this stage does real zip I/O, unlike every earlier byte-buffer
/// based stage).
pub fn collapse_gcode_3mf(
    source: &std::path::Path,
    destination: &std::path::Path,
    line_replacements: &HashMap<usize, Vec<u8>>,
    strip_nonprint_auxiliaries: bool,
) -> Result<CollapseReport, String> {
    let source = std::fs::canonicalize(source).map_err(|e| format!("cannot read source: {e}"))?;
    let destination_abs = if destination.exists() {
        std::fs::canonicalize(destination).map_err(|e| format!("cannot resolve destination: {e}"))?
    } else {
        // `canonicalize` requires the path exist; resolve the parent and
        // rejoin, mirroring Python's `Path(destination).resolve()` which
        // works whether or not the file exists yet.
        let parent = destination.parent().unwrap_or(std::path::Path::new("."));
        let parent = std::fs::canonicalize(parent).map_err(|e| format!("cannot resolve destination directory: {e}"))?;
        parent.join(destination.file_name().ok_or("destination has no file name")?)
    };
    if source == destination_abs {
        return Err("The source sliced file is never overwritten".to_string());
    }

    let file = std::fs::File::open(&source).map_err(|e| format!("cannot open source: {e}"))?;
    let mut archive = zip::ZipArchive::new(file).map_err(|_| "The input is not a valid sliced G-code 3MF".to_string())?;

    let gcode_name = (0..archive.len())
        .map(|i| archive.by_index(i).map(|f| f.name().to_string()))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| format!("corrupt zip entry: {e}"))?
        .into_iter()
        .find(|name| name.to_lowercase().ends_with(".gcode"))
        .ok_or("The package contains no sliced G-code")?;

    let mut report = CollapseReport::default();
    let gcode_bytes = read_entry(&mut archive, &gcode_name)?;
    report.input_gcode_bytes = gcode_bytes.len() as u64;

    let tower_excursions = single_filament::find_tower_excursion_ranges(&gcode_bytes);
    report.tower_excursions_removed = tower_excursions.len() as u64;

    let strip_result = single_filament::strip_marked_blocks(&gcode_bytes, &tower_excursions, line_replacements)?;
    report.toolchanges_removed = strip_result.toolchanges_removed;
    report.tower_blocks_removed = strip_result.tower_blocks_removed;
    report.kept_motion_lines = strip_result.kept_motion_lines;
    report.filament_length_mm = strip_result.filament_length_mm;
    report.orphan_unretractions_removed = strip_result.orphan_unretractions_removed;
    report.groove_segments = strip_result.groove_segments;

    if report.tower_blocks_removed == 0 {
        return Err("No standalone Bambu wipe-tower blocks were found".to_string());
    }
    if report.tower_excursions_removed != report.tower_blocks_removed {
        return Err(format!(
            "Could not identify every complete prime-tower journey ({} journeys for {} standalone prime-tower blocks)",
            report.tower_excursions_removed, report.tower_blocks_removed
        ));
    }

    let metadata_result = metadata::rewrite_gcode_metadata(&strip_result.output, report.filament_length_mm);
    report.output_gcode_bytes = metadata_result.output.len() as u64;
    report.filament_volume_mm3 = metadata_result.filament_volume_mm3;
    report.filament_weight_g = metadata_result.filament_weight_g;
    report.estimated_model_seconds = metadata_result.estimated_model_seconds;
    report.estimated_total_seconds = metadata_result.estimated_total_seconds;
    let digest = metadata_result.md5_hex.into_bytes();

    if let Some(parent) = destination_abs.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("cannot create destination directory: {e}"))?;
    }
    let temp_path = destination_abs.with_extension("tmp-fuzzypaint-engine");
    {
        let temp_file = std::fs::File::create(&temp_path).map_err(|e| format!("cannot create output file: {e}"))?;
        let mut writer = zip::ZipWriter::new(temp_file);
        for index in 0..archive.len() {
            let mut entry = archive.by_index(index).map_err(|e| format!("corrupt zip entry: {e}"))?;
            let name = entry.name().to_string();
            let lower = name.to_lowercase();

            if let Some(settings_index) = filament_settings_index(&lower) {
                if settings_index > 1 {
                    continue;
                }
            }
            if strip_nonprint_auxiliaries && lower.starts_with("auxiliaries/") && !lower.starts_with("auxiliaries/.thumbnails/") {
                report.attachments_removed += 1;
                report.attachment_bytes_removed += entry.size();
                continue;
            }

            let options: zip::write::SimpleFileOptions =
                zip::write::SimpleFileOptions::default().compression_method(entry.compression());

            if name == gcode_name {
                writer.start_file(&name, options).map_err(|e| format!("writing {name}: {e}"))?;
                writer.write_all(&metadata_result.output).map_err(|e| format!("writing {name}: {e}"))?;
                continue;
            }

            let mut data = Vec::new();
            entry.read_to_end(&mut data).map_err(|e| format!("reading {name}: {e}"))?;
            drop(entry);

            if lower.ends_with(".gcode.md5") {
                data = digest.clone();
            } else if lower.ends_with("slice_info.config") {
                data = metadata::rewrite_slice_info(
                    &data,
                    report.filament_weight_g,
                    report.filament_length_mm,
                    report.estimated_total_seconds,
                )?;
            } else if lower.ends_with("plate_1.json") {
                data = metadata::rewrite_plate_json(&data)?;
            } else if lower.ends_with("filament_sequence.json") {
                data = metadata::rewrite_sequence(&data)?;
            } else if lower.ends_with("model_settings.config") {
                data = metadata::rewrite_model_settings(&data)?;
            } else if lower.ends_with("project_settings.config") {
                data = metadata::rewrite_project_settings(&data)?;
            }

            writer.start_file(&name, options).map_err(|e| format!("writing {name}: {e}"))?;
            writer.write_all(&data).map_err(|e| format!("writing {name}: {e}"))?;
        }
        writer.finish().map_err(|e| format!("finishing zip: {e}"))?;
    }
    std::fs::rename(&temp_path, &destination_abs).or_else(|_| {
        // Cross-device or locked-destination fallback: copy then remove.
        std::fs::copy(&temp_path, &destination_abs).map(|_| ()).and_then(|_| std::fs::remove_file(&temp_path))
    }).map_err(|e| format!("cannot write destination: {e}"))?;

    Ok(report)
}

fn read_entry(archive: &mut zip::ZipArchive<std::fs::File>, name: &str) -> Result<Vec<u8>, String> {
    let mut entry = archive.by_name(name).map_err(|e| format!("reading {name}: {e}"))?;
    let mut data = Vec::with_capacity(entry.size() as usize);
    entry.read_to_end(&mut data).map_err(|e| format!("reading {name}: {e}"))?;
    Ok(data)
}
