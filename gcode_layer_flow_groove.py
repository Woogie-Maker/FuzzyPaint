"""Create a one-layer reduced-flow crease at virtual-colour Z transitions."""

from __future__ import annotations

from pathlib import Path

from gcode_layer_groove import find_cross_layer_boundaries
from gcode_single_filament import collapse_gcode_3mf


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--flow", type=float, default=0.55)
    args = parser.parse_args()
    if not 0.1 <= args.flow <= 1.0:
        parser.error("--flow must be between 0.1 and 1.0")
    lines = find_cross_layer_boundaries(args.input)
    print(f"Found {len(lines):,} cross-layer outer-wall segment(s)")
    report = collapse_gcode_3mf(
        args.input,
        args.output,
        extrusion_scales={line: args.flow for line in lines},
    )
    print(
        f"Reduced flow on {report.groove_segments:,} segment(s); removed "
        f"{report.toolchanges_removed} tool changes and {report.tower_blocks_removed} tower blocks; "
        f"estimated filament {report.filament_weight_g:.2f} g"
    )
