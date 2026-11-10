# FuzzyPaint

*For Bambu Studio — turn painted colours into surface texture and
single-filament detail.*

For a file-by-file explanation of the complete GUI workflow, see
[WORKFLOW.md](WORKFLOW.md).

FuzzyPaint converts filament colour painting in a Bambu Studio or OrcaSlicer
`.3mf` project into fuzzy-skin painting. It works directly on the per-triangle
paint masks, so fine brush boundaries and Bambu's internally subdivided
triangles are retained.

## Use on Windows

1. Save the painted model as a **3MF project** in Bambu Studio. STL files do not
   contain painting data.
2. Double-click **Start FuzzyPaint.bat**. The old FuzzyPaint launcher remains
   as a compatibility alias.
3. On **1 Prepare project**, choose the input project and a new output filename.
4. Enter `all`, or specific filament slot numbers such as `2` or `2,4`.
5. Leave **Protect large flat painted surfaces** enabled when the model has
   mortises, mating faces, or smooth mechanical recesses that must retain their
   dimensional fit.
6. **Disable retractions** is enabled by default for the current detailed
   single-filament workflow. It sets the process fallback and every virtual
   filament override to zero before Bambu Studio slices the project.
   **Disable wipe while retracting** is also enabled by default for this
   single-filament workflow. It matters when retractions are enabled again;
   untick it if a particular material or model prints better with wiping.
   **Use sloped Z-hop for every filament** is also enabled by default. It
   changes the lift type while retaining the project’s existing Z-hop heights.
7. Prepare the project. FuzzyPaint selects the new `.3mf` on the second tab.
   By default, the prepared copy omits embedded manuals, creation-kit archives
   and model pictures while retaining thumbnails, geometry and every print
   profile setting. This substantially reduces intermediate file I/O.
8. On **2 Create single-filament file**, use the Balanced preset and create the
   print file. FuzzyPaint runs the installed Bambu Studio slicer automatically
   using the complete print profile stored in the prepared project. The regular
   Bambu Studio window may remain open. The same retraction, wipe and Z-hop
   choices as step 1 are enforced again here, on a temporary slicing copy, so
   older prepared projects do not need the expensive preparation stage
   repeated.
9. The result retains the painted-region toolpath
   segmentation while removing colour swaps and the purge tower.
   Leave **Remove embedded guides, pictures and downloads** enabled. MakerWorld
   projects can contain hundreds of megabytes of non-print attachments that
   otherwise make transfer to the printer unnecessarily slow.
10. Open the processed file and inspect the complete Preview before printing.

If automatic slicing is unavailable, the manual route still works: open the
prepared project in Bambu Studio, slice it, use **Export plate sliced file**,
then choose that `.gcode.3mf` on tab 2.

The original project is never overwritten. By default, the tool **keeps the
original colour painting** and adds fuzzy skin to the same areas. This is the
safest choice for existing multicolour print projects. Tick the removal option
only when colour was used purely as a temporary mask. Existing fuzzy paint on
triangles that have no colour paint is preserved.

Step 1 always retains the temporary colour painting required to build precise
outline detours after slicing. Step 2 removes the resulting filament changes,
purging and prime tower and produces the final single-filament print file.

Requires Python 3.10 or newer. The tool uses only Python's standard library.

## Command line

```powershell
python fuzzypaint.py input.3mf output-fuzzy.3mf
python fuzzypaint.py input.3mf output-fuzzy.3mf --slots 2,4
python fuzzypaint.py input.3mf output-fuzzy.3mf --remove-colour
python fuzzypaint.py input.3mf output-fuzzy.3mf --slots 2 --protect-planar
python fuzzypaint.py input.3mf output-single.3mf --slots 2 --protect-planar --single-colour
```

### Sliced-path outline detours

`gcode_layer_interval_detour.py` operates on Bambu Studio's exported **plate
sliced file** (`.gcode.3mf`). At cross-layer virtual-colour boundaries it reconstructs
the affected outer-wall run as an explicit inward connector, a parallel inset
span, and an outward connector. It then removes the slicer's colour changes and
wipe tower and packages the result as a true single-filament sliced file.

```powershell
python gcode_layer_interval_detour.py input.gcode.3mf output-detour.gcode.3mf --inset 0.24 --boundary-resolution 0.10 --endpoint-coverage 0.21 --same-layer-gap 0 --detour-side both --detour-filaments 2
```

This is a G-code postprocessor. Inspect the complete result in
Bambu Studio Preview before printing; do not use it on an unsliced project 3MF.

## Notes and limitations

- Colour painting means Bambu's `paint_color` face data. Separate coloured
  model parts assigned to different filaments are not paint masks and are not
  converted.
- On a triangle that already has fuzzy paint *and* colour paint, the converted
  colour mask replaces that triangle's old fuzzy mask. Fuzzy paint on all other
  triangles is left alone.
- Planar protection groups adjacent selected triangles whose normals differ by
  no more than 2°, and withholds fuzzy skin from connected patches measuring at
  least 20 mm². It is optional because some designs may genuinely want fuzzy
  skin on a large flat face.
- Preparation and outline analysis are CPU operations. The current converter
  avoids rewriting non-print attachments and caches repeated wall geometry,
  but the detailed cross-layer outline pass is still single-process Python.
  A future layer-boundary multiprocessing implementation is the appropriate
  route to using multiple CPU cores; GPU acceleration is not currently used.
- Apart from the explicitly selected options, the converter retains the
  existing project and print-profile data. With the default retraction option
  enabled, process and per-filament retraction lengths are set to zero before
  slicing; retraction speed and tool-change values are retained. The default
  wipe option changes the stored wipe-on-retraction switches. The default sloped
  Z-hop option changes only the lift type and preserves all Z-hop heights.
  Always verify the sliced preview before a long print.
- The old two-virtual-filament/single-spool experiment is not exposed in the
  GUI. Bambu Studio generates logical filament changes before the Send-window
  AMS mapping, so mapping both colours to one tray cannot remove those changes.

## Tests

```powershell
python -m unittest discover -s tests -v
```
