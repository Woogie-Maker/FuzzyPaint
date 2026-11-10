"""Collapse a Bambu multicolour sliced G-code 3MF to one physical filament.

The model toolpaths are retained verbatim.  Only explicitly delimited Bambu
tool-change and wipe-tower regions are removed, then package metadata is
rewritten to describe a single-filament print.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from fuzzypaint import ConversionError


CP_START = b"; CP TOOLCHANGE START"
CP_END = b"; CP TOOLCHANGE END"
TOWER_START = b"; WIPE_TOWER_START"
TOWER_END = b"; WIPE_TOWER_END"
MOVE_RE = re.compile(rb"^G[0123](?:\s|$)", re.IGNORECASE)
E_RE = re.compile(rb"(?:^|\s)E(-?(?:\d+(?:\.\d*)?|\.\d+))(?:\s|$)", re.IGNORECASE)
XYE_FIELD_RE = re.compile(
    rb"(?:^|\s)([XYE])(-?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE
)
MOTION_FIELD_RE = re.compile(
    rb"(?:^|\s)([XYZEIJFSP])(-?(?:\d+(?:\.\d*)?|\.\d+))", re.IGNORECASE
)


@dataclass
class CollapseReport:
    toolchanges_removed: int = 0
    tower_blocks_removed: int = 0
    kept_motion_lines: int = 0
    filament_length_mm: float = 0.0
    filament_volume_mm3: float = 0.0
    filament_weight_g: float = 0.0
    input_gcode_bytes: int = 0
    output_gcode_bytes: int = 0
    groove_segments: int = 0
    attachments_removed: int = 0
    attachment_bytes_removed: int = 0
    tower_excursions_removed: int = 0
    orphan_unretractions_removed: int = 0
    estimated_model_seconds: float = 0.0
    estimated_total_seconds: float = 0.0


def strip_nonprint_attachments(
    source: os.PathLike | str, destination: os.PathLike | str
) -> tuple[int, int]:
    """Copy a sliced package without MakerWorld documents and media assets."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ConversionError("The source sliced file is never overwritten")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    removed = removed_bytes = 0
    try:
        with zipfile.ZipFile(source_path) as archive:
            bad = archive.testzip()
            if bad:
                raise ConversionError(f"The sliced 3MF is corrupt at {bad}")
            fd, temporary = tempfile.mkstemp(
                prefix=destination_path.stem + "-", suffix=".tmp", dir=destination_path.parent
            )
            os.close(fd)
            with zipfile.ZipFile(temporary, "w", allowZip64=True) as output:
                output.comment = archive.comment
                for info in archive.infolist():
                    lower = info.filename.lower()
                    if (
                        lower.startswith("auxiliaries/")
                        and not lower.startswith("auxiliaries/.thumbnails/")
                    ):
                        removed += 1
                        removed_bytes += info.file_size
                        continue
                    output.writestr(info, archive.read(info))
        os.replace(temporary, destination_path)
        temporary = None
        return removed, removed_bytes
    except zipfile.BadZipFile as exc:
        raise ConversionError("The input is not a valid sliced G-code 3MF") from exc
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _strip_marked_blocks(
    source,
    destination,
    report: CollapseReport,
    groove_lines: set[int] | None = None,
    coordinate_overrides: dict[int, tuple[float, float]] | None = None,
    extrusion_scales: dict[int, float] | None = None,
    line_replacements: dict[int, bytes] | None = None,
    skip_ranges: list[tuple[int, int]] | None = None,
) -> None:
    in_cp = False
    in_tower = False
    groove_lines = groove_lines or set()
    coordinate_overrides = coordinate_overrides or {}
    extrusion_scales = extrusion_scales or {}
    line_replacements = line_replacements or {}
    range_iterator = iter(skip_ranges or [])
    current_range = next(range_iterator, None)
    current_range_e = 0.0
    pending_unretract = False
    for line_number, line in enumerate(source, 1):
        stripped = line.strip()
        if stripped == CP_START:
            if in_cp or in_tower:
                raise ConversionError("Nested or overlapping tool-change markers")
            in_cp = True
            report.toolchanges_removed += 1
            continue
        if stripped == CP_END:
            if not in_cp:
                raise ConversionError("Tool-change end marker without a start marker")
            in_cp = False
            continue
        if in_cp:
            continue
        if stripped == TOWER_START:
            if in_tower:
                raise ConversionError("Nested wipe-tower markers")
            in_tower = True
            report.tower_blocks_removed += 1
            continue
        if stripped == TOWER_END:
            if not in_tower:
                raise ConversionError("Wipe-tower end marker without a start marker")
            in_tower = False
            continue
        if in_tower:
            continue

        while current_range and line_number > current_range[1]:
            # A standalone tower excursion ends retracted, then returns to the
            # model and unretracts.  Removing the excursion but retaining that
            # final E+ move injects a full retraction-length blob into the
            # model.  Keep the necessary XY return travel, but suppress its
            # now-orphaned pure-E unretraction below.
            pending_unretract = current_range_e < -0.1
            current_range_e = 0.0
            current_range = next(range_iterator, None)
        if current_range and current_range[0] <= line_number <= current_range[1]:
            skipped_e = E_RE.search(stripped)
            if skipped_e:
                current_range_e += float(skipped_e.group(1))
            continue

        if pending_unretract:
            pure_e = re.match(
                rb"^G1\s+E(?P<value>(?:\d+(?:\.\d*)?|\.\d+))(?:\s+F\d+(?:\.\d*)?)?(?:\s*;.*)?$",
                stripped,
                re.IGNORECASE,
            )
            if pure_e and float(pure_e.group("value")) > 0.0:
                pending_unretract = False
                report.orphan_unretractions_removed += 1
                continue
            if stripped.startswith(b"; FEATURE:") or (
                MOVE_RE.match(stripped) and E_RE.search(stripped)
            ):
                raise ConversionError(
                    "A removed tower trip was not followed by its expected unretraction"
                )

        if line_number in groove_lines:
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            content = line[: -len(ending)] if ending else line
            content = re.sub(
                rb"\s+E-?(?:\d+(?:\.\d*)?|\.\d+)(?=\s|$)", b"", content, count=1
            )
            line = content + b" ; FuzzyPaint one-layer groove" + ending
            report.groove_segments += 1

        if line_number in coordinate_overrides:
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            content = line[: -len(ending)] if ending else line
            target_x, target_y = coordinate_overrides[line_number]
            if re.search(rb"(?:^|\s)X-?(?:\d+(?:\.\d*)?|\.\d+)", content):
                content = re.sub(
                    rb"(?<=\s)X-?(?:\d+(?:\.\d*)?|\.\d+)",
                    f"X{target_x:.5f}".encode(),
                    content,
                    count=1,
                )
            if re.search(rb"(?:^|\s)Y-?(?:\d+(?:\.\d*)?|\.\d+)", content):
                content = re.sub(
                    rb"(?<=\s)Y-?(?:\d+(?:\.\d*)?|\.\d+)",
                    f"Y{target_y:.5f}".encode(),
                    content,
                    count=1,
                )
            line = content + b" ; FuzzyPaint cross-layer inset" + ending
            report.groove_segments += 1

        if line_number in extrusion_scales:
            factor = extrusion_scales[line_number]
            if not 0.0 < factor <= 1.0:
                raise ConversionError("Extrusion scale must be greater than 0 and at most 1")
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            content = line[: -len(ending)] if ending else line
            match = re.search(
                rb"(?P<prefix>\sE)(?P<value>-?(?:\d+(?:\.\d*)?|\.\d+))(?=\s|$)",
                content,
            )
            if not match:
                raise ConversionError(f"Selected flow-groove line {line_number} has no extrusion")
            scaled = float(match.group("value")) * factor
            content = content[: match.start("value")] + f"{scaled:.6f}".encode() + content[match.end("value") :]
            line = content + f" ; FuzzyPaint flow groove {factor:.0%}".encode() + ending
            report.groove_segments += 1

        if line_number in line_replacements:
            line = line_replacements[line_number]
            if not line.endswith(b"\n"):
                line += b"\n"
            report.groove_segments += 1

        destination.write(line)
        for emitted in line.splitlines():
            stripped_written = emitted.strip()
            if MOVE_RE.match(stripped_written):
                report.kept_motion_lines += 1
                match = E_RE.search(stripped_written)
                if match:
                    report.filament_length_mm += float(match.group(1))

    if in_cp or in_tower:
        raise ConversionError("The G-code ends inside a marked tool-change/tower block")
    if report.toolchanges_removed == 0:
        raise ConversionError("No Bambu tool-change blocks were found")
    # Each change contains a nested tower marker that is intentionally skipped
    # as part of the whole CP block; the standalone tower count is therefore


def _find_tower_excursion_ranges(source) -> list[tuple[int, int]]:
    """Find the unmarked travel wrappers around Bambu wipe-tower blocks.

    Bambu's routing around a tower is not stable: wipe settings, sloped Z-hop,
    and ``avoid crossing walls`` can turn one long move into several shorter
    moves and can remove the ``WIPE_END`` marker entirely.  Learn the model and
    tower footprints first, then regard the first travel that leaves the model
    footprint as the start of an excursion.  The range ends immediately before
    the next object marker, leaving the model-side return, Z drop and matched
    unretraction intact.
    """
    # First learn the tower's actual XY bounds from explicitly marked tower
    # moves, and the model footprint from genuine post-OBJECT_ID extrusion.
    tower_points: list[tuple[float, float]] = []
    model_points: list[tuple[float, float]] = []
    x = y = 0.0
    relative_xy = False
    in_cp = in_tower = False
    feature = b""
    have_object = False
    for line in source:
        stripped = line.strip()
        if stripped == CP_START:
            in_cp = True
        elif stripped == CP_END:
            in_cp = False
        elif stripped == TOWER_START and not in_cp:
            in_tower = True
        elif stripped == TOWER_END and not in_cp:
            in_tower = False
        elif stripped.startswith(b"; OBJECT_ID:") and not in_cp:
            have_object = True
        if stripped.startswith(b"; FEATURE:"):
            feature = stripped.split(b":", 1)[1].strip().lower()
        if stripped == b"G90":
            relative_xy = False
        elif stripped == b"G91":
            relative_xy = True
        command = stripped.split(None, 1)[0].upper() if stripped else b""
        if command in (b"G0", b"G1", b"G2", b"G3"):
            fields = {key.upper(): float(value) for key, value in XYE_FIELD_RE.findall(stripped)}
            nx = x + fields.get(b"X", 0.0) if relative_xy else fields.get(b"X", x)
            ny = y + fields.get(b"Y", 0.0) if relative_xy else fields.get(b"Y", y)
            if in_tower and (b"X" in fields or b"Y" in fields):
                tower_points.append((nx, ny))
            if (
                have_object
                and not in_cp
                and not in_tower
                and feature != b"prime tower"
                and fields.get(b"E", 0.0) > 0.0
                and (b"X" in fields or b"Y" in fields)
            ):
                model_points.append((x, y))
                model_points.append((nx, ny))
            x, y = nx, ny
    source.seek(0)
    tower_bounds = None
    if tower_points:
        tower_bounds = (
            min(point[0] for point in tower_points) - 10.0,
            max(point[0] for point in tower_points) + 10.0,
            min(point[1] for point in tower_points) - 10.0,
            max(point[1] for point in tower_points) + 10.0,
        )
    model_bounds = None
    if model_points:
        # Ten millimetres comfortably contains local Z-hop and wall-avoidance
        # routing without reaching a normally separated prime tower.
        model_bounds = (
            min(point[0] for point in model_points) - 10.0,
            max(point[0] for point in model_points) + 10.0,
            min(point[1] for point in model_points) - 10.0,
            max(point[1] for point in model_points) + 10.0,
        )

    ranges: list[tuple[int, int]] = []
    x = y = 0.0
    relative_xy = False
    in_cp = False
    in_tower = False
    candidate_start: int | None = None
    active_start: int | None = None
    for line_number, line in enumerate(source, 1):
        stripped = line.strip()
        if stripped == CP_START:
            if active_start is None and candidate_start is not None:
                # Some Bambu profiles enter the tool-change block directly;
                # there is no preceding standalone WIPE_TOWER_START on these
                # layers.  The already-detected departure still belongs to the
                # same complete tower excursion.
                active_start = candidate_start
            in_cp = True
        if stripped == TOWER_START and not in_cp:
            in_tower = True
        elif stripped == TOWER_END and not in_cp:
            in_tower = False
        if active_start is not None and not in_cp and stripped.startswith(b"; OBJECT_ID:"):
            ranges.append((active_start, line_number - 1))
            active_start = None
            candidate_start = None
        if (
            stripped == TOWER_START
            and not in_cp
            and active_start is None
            and candidate_start is not None
        ):
            active_start = candidate_start
        if stripped == b"G90":
            relative_xy = False
        elif stripped == b"G91":
            relative_xy = True
        command = stripped.split(None, 1)[0].upper() if stripped else b""
        if command in (b"G0", b"G1", b"G2", b"G3"):
            fields = {key.upper(): float(value) for key, value in XYE_FIELD_RE.findall(stripped)}
            nx = x + fields.get(b"X", 0.0) if relative_xy else fields.get(b"X", x)
            ny = y + fields.get(b"Y", 0.0) if relative_xy else fields.get(b"Y", y)
            near_tower = (
                tower_bounds is not None
                and tower_bounds[0] <= nx <= tower_bounds[1]
                and tower_bounds[2] <= ny <= tower_bounds[3]
            )
            outside_model = (
                model_bounds is not None
                and not (
                    model_bounds[0] <= nx <= model_bounds[1]
                    and model_bounds[2] <= ny <= model_bounds[3]
                )
            )
            if (
                active_start is None
                and not in_cp
                and not in_tower
                and fields.get(b"E", 0.0) <= 0.0
                and (
                    outside_model
                    # Fallback for tiny/synthetic G-code without enough model
                    # extrusion to establish a footprint.
                    or (
                        model_bounds is None
                        and near_tower
                        and math.hypot(nx - x, ny - y) > 30.0
                    )
                )
            ):
                if candidate_start is None:
                    candidate_start = line_number
            elif (
                active_start is None
                and not in_cp
                and not in_tower
                and fields.get(b"E", 0.0) > 0.0
                and (b"X" in fields or b"Y" in fields)
            ):
                # A real model extrusion means a previous departure candidate
                # was merely local wall-avoidance travel.
                candidate_start = None
            x, y = nx, ny
        if stripped == CP_END:
            in_cp = False
    return ranges


def _validate_tower_excursions(report: CollapseReport) -> None:
    """Require one removed travel wrapper per standalone tower block.

    A prime tower may be printed on layers that contain no colour change, so
    its number of journeys is not necessarily the number of CP TOOLCHANGE
    blocks.  The reliable one-to-one relationship is with the standalone
    WIPE_TOWER blocks that remain outside those nested tool-change blocks.
    """
    if report.tower_blocks_removed == 0:
        raise ConversionError("No standalone Bambu wipe-tower blocks were found")
    if report.tower_excursions_removed != report.tower_blocks_removed:
        raise ConversionError(
            "Could not identify every complete prime-tower journey "
            f"({report.tower_excursions_removed} journeys for "
            f"{report.tower_blocks_removed} standalone prime-tower blocks)"
        )


def _parse_time_text(value: bytes) -> int:
    hours = re.search(rb"(\d+)h", value)
    minutes = re.search(rb"(\d+)m", value)
    seconds = re.search(rb"(\d+)s", value)
    return (
        (int(hours.group(1)) * 3600 if hours else 0)
        + (int(minutes.group(1)) * 60 if minutes else 0)
        + (int(seconds.group(1)) if seconds else 0)
    )


def _format_time(seconds: float) -> str:
    value = max(0, round(seconds))
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    return (f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s")


def _estimate_progress_times(source) -> tuple[float, float, dict[int, float]]:
    """Estimate post-processed model time and time remaining at each M73 line.

    The second estimate treats every short move as stopping; the first ignores
    acceleration.  Their blend closely follows Bambu's planner for the highly
    segmented paths this converter produces, while remaining deterministic.
    """
    start = source.tell()
    x = y = z = e = 0.0
    feed = 3000.0
    acceleration = 500.0
    relative_xyz = False
    relative_e = True
    started = False
    naive = stopped = 0.0
    checkpoints: dict[int, tuple[float, float]] = {}
    preparation_seconds = 0.0
    for line_number, raw in enumerate(source, 1):
        stripped = raw.strip()
        if stripped.startswith(b"; model printing time:"):
            match = re.search(
                rb"model printing time:\s*(.*?);\s*total estimated time:\s*(.*)$",
                stripped,
                re.IGNORECASE,
            )
            if match:
                preparation_seconds = max(
                    0, _parse_time_text(match.group(2)) - _parse_time_text(match.group(1))
                )
        if re.match(rb"^M73\s+L1(?:\s|$)", stripped):
            started = True
        if re.match(rb"^M73\s+P\d+", stripped):
            checkpoints[line_number] = (naive, stopped)
        if stripped == b"G90":
            relative_xyz = False
            continue
        if stripped == b"G91":
            relative_xyz = True
            continue
        if stripped == b"M82":
            relative_e = False
            continue
        if stripped == b"M83":
            relative_e = True
            continue
        accel_match = re.match(rb"^M204\s+S([0-9.]+)", stripped)
        if accel_match:
            acceleration = max(float(accel_match.group(1)), 1.0)
            continue
        command = stripped.split(None, 1)[0].upper() if stripped else b""
        fields = {key.upper(): float(value) for key, value in MOTION_FIELD_RE.findall(stripped)}
        if b"F" in fields:
            feed = fields[b"F"]
        if command == b"G92":
            x, y, z, e = (
                fields.get(b"X", x), fields.get(b"Y", y),
                fields.get(b"Z", z), fields.get(b"E", e),
            )
            continue
        if command == b"G4" and started:
            dwell = fields.get(b"S", 0.0) + fields.get(b"P", 0.0) / 1000.0
            naive += dwell
            stopped += dwell
            continue
        if command not in (b"G0", b"G1", b"G2", b"G3"):
            continue
        nx = x + fields.get(b"X", 0.0) if relative_xyz else fields.get(b"X", x)
        ny = y + fields.get(b"Y", 0.0) if relative_xyz else fields.get(b"Y", y)
        nz = z + fields.get(b"Z", 0.0) if relative_xyz else fields.get(b"Z", z)
        ne = e + fields.get(b"E", 0.0) if relative_e else fields.get(b"E", e)
        if command in (b"G2", b"G3") and (b"I" in fields or b"J" in fields):
            cx, cy = x + fields.get(b"I", 0.0), y + fields.get(b"J", 0.0)
            radius = math.hypot(x - cx, y - cy)
            first_angle = math.atan2(y - cy, x - cx)
            last_angle = math.atan2(ny - cy, nx - cx)
            sweep = (
                (last_angle - first_angle) % (2 * math.pi)
                if command == b"G3"
                else (first_angle - last_angle) % (2 * math.pi)
            )
            distance = abs(radius * sweep)
        else:
            distance = math.sqrt((nx - x) ** 2 + (ny - y) ** 2 + (nz - z) ** 2)
        if distance < 1e-9 and b"E" in fields:
            distance = abs(ne - e)
        if started and distance > 0.0:
            speed = max(feed / 60.0, 1e-6)
            naive += distance / speed
            critical = speed * speed / acceleration
            stopped += (
                2.0 * math.sqrt(distance / acceleration)
                if distance < critical
                else 2.0 * speed / acceleration + (distance - critical) / speed
            )
        x, y, z, e = nx, ny, nz, ne
    source.seek(start)
    blend = 0.76
    model_seconds = naive + blend * (stopped - naive)
    remaining = {
        line: max(0.0, model_seconds - (n + blend * (s - n)))
        for line, (n, s) in checkpoints.items()
    }
    return model_seconds, preparation_seconds, remaining


def _rewrite_gcode_metadata(source, destination, report: CollapseReport) -> bytes:
    diameter = 1.75
    density = 1.32
    report.filament_volume_mm3 = report.filament_length_mm * math.pi * (diameter / 2.0) ** 2
    report.filament_weight_g = report.filament_volume_mm3 / 1000.0 * density

    replacements = {
        b"; total filament length [mm] :": f"; total filament length [mm] : {report.filament_length_mm:.2f}".encode(),
        b"; total filament volume [cm^3] :": f"; total filament volume [cm^3] : {report.filament_volume_mm3:.2f}".encode(),
        b"; total filament weight [g] :": f"; total filament weight [g] : {report.filament_weight_g:.2f}".encode(),
        b"; filament:": b"; filament: 1",
        b"; enable_prime_tower =": b"; enable_prime_tower = 0",
    }

    # Bambu's M73 R values are minute estimates baked into the original
    # multicolour slice.  They become actively misleading after the purge and
    # tower moves have been removed (the printer can even display a negative
    # remaining time).  Retain useful layer-based progress, but deliberately
    # omit the now-invalid remaining-time field.
    total_layers = 0
    model_seconds, preparation_seconds, remaining_by_line = _estimate_progress_times(source)
    report.estimated_model_seconds = model_seconds
    report.estimated_total_seconds = model_seconds + preparation_seconds
    if source.seekable():
        start = source.tell()
        for scan_line in source:
            match = re.match(rb"^M73\s+L(\d+)(?:\s|$)", scan_line.strip())
            if match:
                total_layers = max(total_layers, int(match.group(1)))
        source.seek(start)

    md5 = hashlib.md5()
    current_layer = 0
    single_value_metadata = {
        b"; filament_map =": b",",
        b"; filament_nozzle_map =": b",",
        b"; filament_colour =": b";",
        b"; filament_colour_type =": b";",
        b"; filament_type =": b";",
        b"; filament_settings_id =": b";",
        b"; filament_ids =": b";",
    }
    for line_number, line in enumerate(source, 1):
        ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
        content = line[: -len(ending)] if ending else line

        # The normal CP TOOLCHANGE blocks are removed earlier, but Bambu also
        # emits an initial AMS selection in machine-start G-code.  A source job
        # whose first printed colour was virtual filament 2 therefore retains
        # M620 S1A / T1 / M621 S1A unless it is explicitly collapsed here.
        # Leaving that block behind makes a nominally single-filament package
        # enter (and sometimes stall in) an obsolete AMS loading operation.
        ams_select = re.match(rb"^(\s*)(M62[01])(\s+)S(\d+)A(.*)$", content, re.IGNORECASE)
        if ams_select and int(ams_select.group(4)) < 255:
            content = (
                ams_select.group(1) + ams_select.group(2) + ams_select.group(3)
                + b"S0A" + ams_select.group(5)
            )
        tool_select = re.match(rb"^(\s*)T(\d+)(\s*(?:;.*)?)$", content, re.IGNORECASE)
        if tool_select and int(tool_select.group(2)) < 255:
            content = tool_select.group(1) + b"T0" + tool_select.group(3)

        layer_match = re.match(rb"^M73\s+L(\d+)(?:\s|$)", content)
        if layer_match:
            current_layer = int(layer_match.group(1))
        progress_match = re.match(rb"^M73\s+P\d+(?:\s+R-?\d+)?(?P<tail>\s*;.*)?$", content)
        if progress_match and total_layers:
            percent = max(0, min(100, round(current_layer * 100 / total_layers)))
            remaining = remaining_by_line.get(line_number, 0.0)
            if current_layer == 0:
                remaining += preparation_seconds
            minutes = 0 if percent >= 100 else max(1, math.ceil(remaining / 60.0))
            content = f"M73 P{percent} R{minutes}".encode() + (progress_match.group("tail") or b"")
        for prefix, replacement in replacements.items():
            if content.startswith(prefix):
                content = replacement
                break
        for prefix, delimiter in single_value_metadata.items():
            if content.startswith(prefix):
                first = content.split(b"=", 1)[1].strip().split(delimiter, 1)[0].strip()
                content = prefix + b" " + first
                break
        if content.startswith(b"; model printing time:"):
            content = (
                f"; model printing time: {_format_time(model_seconds)}; "
                f"total estimated time: {_format_time(model_seconds + preparation_seconds)}"
            ).encode()
        written = content + ending
        destination.write(written)
        md5.update(written)
    return md5.hexdigest().encode("ascii")


def _rewrite_slice_info(data: bytes, report: CollapseReport) -> bytes:
    root = ET.fromstring(data)
    plate = root.find("plate")
    if plate is None:
        raise ConversionError("slice_info.config contains no plate")
    for metadata in plate.findall("metadata"):
        if metadata.get("key") == "weight":
            metadata.set("value", f"{report.filament_weight_g:.2f}")
        elif metadata.get("key") == "prediction":
            metadata.set("value", str(round(report.estimated_total_seconds)))
        elif metadata.get("key") == "filament_maps":
            metadata.set("value", "1")
        elif metadata.get("key") == "limit_filament_maps":
            metadata.set("value", "0")
    filaments = plate.findall("filament")
    if not filaments:
        raise ConversionError("slice_info.config contains no filament")
    first = filaments[0]
    first.set("id", "1")
    first.set("used_m", f"{report.filament_length_mm / 1000.0:.2f}")
    first.set("used_g", f"{report.filament_weight_g:.2f}")
    for filament in filaments[1:]:
        plate.remove(filament)
    lists = plate.find("layer_filament_lists")
    if lists is not None:
        for item in lists.findall("layer_filament_list"):
            item.set("filament_list", "0")
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _rewrite_plate_json(data: bytes) -> bytes:
    value = json.loads(data.decode("utf-8-sig"))
    colors = value.get("filament_colors")
    value["filament_colors"] = colors[:1] if isinstance(colors, list) and colors else ["#757575"]
    value["filament_ids"] = [0]
    value["first_extruder"] = 0
    objects = value.get("bbox_objects")
    if isinstance(objects, list):
        value["bbox_objects"] = [item for item in objects if item.get("name") != "wipe_tower"]
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _rewrite_sequence(data: bytes) -> bytes:
    value = json.loads(data.decode("utf-8-sig"))
    for plate in value.values():
        sequence = plate.get("sequence")
        if isinstance(sequence, list):
            plate["sequence"] = [1] * len(sequence)
        nozzle = plate.get("nozzle_sequence")
        if isinstance(nozzle, list):
            plate["nozzle_sequence"] = [0] * len(nozzle)
        assignment = plate.get("optimal_assignment")
        if isinstance(assignment, list):
            plate["optimal_assignment"] = [0]
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _rewrite_model_settings(data: bytes) -> bytes:
    """Collapse the plate-level virtual-filament mapping to one entry."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ConversionError("Could not read model_settings.config") from exc
    plate = root.find("plate")
    if plate is not None:
        for metadata in plate.findall("metadata"):
            if metadata.get("key") == "filament_maps":
                metadata.set("value", "1")
            elif metadata.get("key") == "filament_volume_maps":
                metadata.set("value", "0")
    ET.indent(root, space="  ")
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8") + b"\n"


def _rewrite_project_settings(data: bytes) -> bytes:
    """Make the editable project state agree with the already-collapsed G-code."""
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionError("Could not read project_settings.config") from exc
    if not isinstance(value, dict):
        raise ConversionError("project_settings.config is not a JSON object")

    colours = value.get("filament_colour")
    filament_count = len(colours) if isinstance(colours, list) else 0
    if filament_count > 1:
        for key, setting in list(value.items()):
            if not isinstance(setting, list) or not setting:
                continue
            if key in {"different_settings_to_system", "inherits_group"} and len(setting) == filament_count + 2:
                # These groups are process, one entry per filament, printer.
                value[key] = [setting[0], setting[1], setting[-1]]
            elif key == "flush_volumes_matrix" and len(setting) == filament_count * filament_count:
                value[key] = [setting[0]]
            elif len(setting) == filament_count:
                value[key] = [setting[0]]
            elif len(setting) == filament_count * 2:
                # Bambu 2.8 stores Standard/High-Flow values for each filament.
                value[key] = setting[:2]
            elif len(setting) == filament_count * 4:
                value[key] = setting[:4]
    value["enable_prime_tower"] = "0"
    minimal = value.get("filament_minimal_purge_on_wipe_tower")
    if isinstance(minimal, list):
        value["filament_minimal_purge_on_wipe_tower"] = ["0"] * len(minimal)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def collapse_gcode_3mf(
    source: os.PathLike | str,
    destination: os.PathLike | str,
    *,
    groove_lines: set[int] | None = None,
    coordinate_overrides: dict[int, tuple[float, float]] | None = None,
    extrusion_scales: dict[int, float] | None = None,
    line_replacements: dict[int, bytes] | None = None,
    strip_nonprint_auxiliaries: bool = False,
) -> CollapseReport:
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ConversionError("The source sliced file is never overwritten")
    report = CollapseReport()
    temporary_paths: list[str] = []
    try:
        with zipfile.ZipFile(source_path) as archive:
            bad = archive.testzip()
            if bad:
                raise ConversionError(f"The sliced 3MF is corrupt at {bad}")
            infos = archive.infolist()
            gcode_name = next((i.filename for i in infos if i.filename.lower().endswith(".gcode")), None)
            if not gcode_name:
                raise ConversionError("The package contains no sliced G-code")
            report.input_gcode_bytes = archive.getinfo(gcode_name).file_size

            with archive.open(gcode_name) as source_gcode:
                tower_excursions = _find_tower_excursion_ranges(source_gcode)
            report.tower_excursions_removed = len(tower_excursions)

            fd, stripped_name = tempfile.mkstemp(suffix=".gcode", dir=destination_path.parent)
            os.close(fd)
            temporary_paths.append(stripped_name)
            with archive.open(gcode_name) as source_gcode, open(stripped_name, "wb") as stripped:
                _strip_marked_blocks(
                    source_gcode,
                    stripped,
                    report,
                    groove_lines,
                    coordinate_overrides,
                    extrusion_scales,
                    line_replacements,
                    tower_excursions,
                )
            _validate_tower_excursions(report)

            fd, final_name = tempfile.mkstemp(suffix=".gcode", dir=destination_path.parent)
            os.close(fd)
            temporary_paths.append(final_name)
            with open(stripped_name, "rb") as stripped, open(final_name, "wb") as final:
                digest = _rewrite_gcode_metadata(stripped, final, report)
            report.output_gcode_bytes = os.path.getsize(final_name)

            destination_path.parent.mkdir(parents=True, exist_ok=True)
            fd, package_name = tempfile.mkstemp(suffix=".tmp", dir=destination_path.parent)
            os.close(fd)
            temporary_paths.append(package_name)
            with zipfile.ZipFile(package_name, "w", allowZip64=True) as output:
                for info in infos:
                    lower = info.filename.lower()
                    filament_config = re.fullmatch(r"metadata/filament_settings_(\d+)\.config", lower)
                    if filament_config and int(filament_config.group(1)) > 1:
                        continue
                    if (
                        strip_nonprint_auxiliaries
                        and lower.startswith("auxiliaries/")
                        and not lower.startswith("auxiliaries/.thumbnails/")
                    ):
                        report.attachments_removed += 1
                        report.attachment_bytes_removed += info.file_size
                        continue
                    if info.filename == gcode_name:
                        output.write(final_name, info.filename, compress_type=info.compress_type)
                        continue
                    data = archive.read(info)
                    if lower.endswith(".gcode.md5"):
                        data = digest
                    elif lower.endswith("slice_info.config"):
                        data = _rewrite_slice_info(data, report)
                    elif lower.endswith("plate_1.json"):
                        data = _rewrite_plate_json(data)
                    elif lower.endswith("filament_sequence.json"):
                        data = _rewrite_sequence(data)
                    elif lower.endswith("model_settings.config"):
                        data = _rewrite_model_settings(data)
                    elif lower.endswith("project_settings.config"):
                        data = _rewrite_project_settings(data)
                    output.writestr(info, data)
            os.replace(package_name, destination_path)
            temporary_paths.remove(package_name)
        return report
    except zipfile.BadZipFile as exc:
        raise ConversionError("The input is not a valid sliced G-code 3MF") from exc
    finally:
        for name in temporary_paths:
            if os.path.exists(name):
                os.unlink(name)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Collapse Bambu sliced multicolour G-code to one filament")
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = collapse_gcode_3mf(args.input, args.output)
    print(
        f"Removed {result.toolchanges_removed} tool changes and "
        f"{result.tower_blocks_removed} standalone tower blocks; "
        f"estimated filament {result.filament_weight_g:.2f} g"
    )
