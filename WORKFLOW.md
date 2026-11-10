# FuzzyPaint workflow

This guide describes the two-stage workflow and the purpose of every file. The
source project is never overwritten.

## The short version

```text
Colour-painted project .3mf
        │
        ▼
FuzzyPaint — 1 Prepare project
        │
        ▼
Prepared editable project .3mf
        │
        ▼
FuzzyPaint — 2 Create single-filament file
        │ (runs Bambu Studio's slicer automatically)
        │
        ▼
Finished single-filament .gcode.3mf → preview, then print
```

## Step 1: prepare the editable project

In **1 Prepare project**:

1. Choose a colour-painted Bambu Studio project (`.3mf`). This may be the
   original project or a previously prepared editable project. Do not choose a
   `.gcode.3mf` here.
2. Choose the painted colour slot used as the texture mask, such as `2`.
3. Choose a new output filename. For example:
   `RelicRush_BridgeTower (C2F-Prepped).3mf`.
4. Enable the advanced settings when changing fuzzy texture. A useful starting
   point for dense moss with a 0.4 mm nozzle is:
   - Thickness: `0.25 mm`
   - Point distance: `0.05 mm`
   - Noise type: `classic`
5. Click **Prepare file for slicing**.

Thickness controls the maximum sideways displacement perpendicular to the
wall. Point distance controls how frequently the path chooses another displaced
point as it travels along the wall. A smaller point distance produces more
rapid in/out movement and more G-code.

## Automatic slicing (recommended)

After step 1, FuzzyPaint selects the prepared project on tab 2. Click
**Create single-filament print file**. FuzzyPaint invokes the installed Bambu
Studio slicer using the printer, filament and process profiles already stored
inside that project, then immediately performs the single-filament conversion.
The regular Bambu Studio window may remain open while this runs.

**Disable retractions**, **disable wipe while retracting** and **use sloped
Z-hop before automatic slicing** are all enabled by default — this single-
filament workflow generally works best with them off, but they remain
togglable here in case a particular material or model prints better with one
re-enabled. Whatever is checked is applied to a temporary slicing copy, so it
also works with an older prepared project without rerunning step 1.

## Manual slicing and export (fallback)

1. Open the prepared `.3mf` created by step 1.
2. Slice the plate normally.
3. Inspect the multicolour Preview. At this stage, colour changes and a prime
   tower are expected; step 2 removes them.
4. Use **Print plate ▼ → Export plate sliced file**.

There is no required intermediate Save command after slicing. Saving the
editable project is optional. For the exported sliced file, any clear name is
valid, for example:

`RelicRush_BridgeTower (C2F-Created).gcode.3mf`

Bambu Studio may append the plate name, producing something such as
`RelicRush_BridgeTower (C2F-Created)_Bridge Tower.gcode.3mf`. That is harmless.
FuzzyPaint removes a repeated plate name from its suggested final filename.

## Step 2: create the printable single-filament file

In **2 Create single-filament file**:

1. Choose either the prepared `(C2F-Prepped).3mf` project for automatic slicing,
   or a raw plate-sliced `.gcode.3mf` exported manually from Bambu Studio.
2. Select the painted colour slot whose boundaries should become physical
   outline detours.
3. Normally leave **Balanced — proven settings**, **Both — complete outline**,
   and attachment removal selected.
4. Click **Create single-filament print file**.

The finished file should normally end in
`(C2F-Print).gcode.3mf`. Open it in Bambu Studio and verify:

- Filament change times: `0`
- Only one model filament is used
- No prime tower toolpaths remain
- Outline detours and fuzzy texture look correct across the complete Preview

Do not put the finished `(C2F-Print).gcode.3mf` through step 2 a
second time. Its temporary colour-change information has already been removed.

## File types at a glance

| File | Meaning | Where it goes next |
|---|---|---|
| Original colour-painted `.3mf` | Editable source project | FuzzyPaint step 1 |
| Prepared `.3mf` | Editable project containing fuzzy paint and temporary colour boundaries | FuzzyPaint step 2 (automatic) or Bambu Studio (manual fallback) |
| Raw exported `.gcode.3mf` | Multicolour sliced package; colour changes are expected | FuzzyPaint step 2 |
| `(C2F-Print).gcode.3mf` | Finished, already sliced single-filament print | Bambu Studio Preview, then printer |

## Naming convention

- Step 1 output: `file_name (C2F-Prepped).3mf`
- Optional manual Bambu Studio sliced export: `file_name (C2F-Created).gcode.3mf`
- Step 2 printable output: `file_name (C2F-Print).gcode.3mf`

The latter two are `.gcode.3mf` packages because they contain already-sliced
printer G-code. The tool suggests these names automatically where it controls
the Save dialog, and shows the recommended Created name before the Bambu Studio
export step.
