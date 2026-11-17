# Changelog

Version numbers follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — incompatible changes (e.g. a file format change, a removed workflow).
- **MINOR** — new backward-compatible functionality (a new toolbar, a new setting).
- **PATCH** — backward-compatible bug fixes only.

The version bump happens in the same commit as the change it describes, not as a
separate afterthought -- that's what let `APP_VERSION` drift and sit at 0.2.6
through several real changes before this file existed.

## 0.12.0

- Added an MIT software licence, packaged the licence notice with Windows
  builds, and exposed the full terms from the About window.
- Identified the Python/Rust architecture prominently in the README and About
  window, including whether the bundled PyO3 Rust engine is active.
- Added GitHub and licence buttons to the About window. The existing toolbar
  GitHub shortcut remains available.
- Migrated the Windows application and bundled Rust extension to Python 3.15.2
  and PyO3 0.30.1 without changing conversion defaults or output behaviour.
- Reused an existing Explorer window when it already shows the exact output
  folder. Matching uses canonical, case-insensitive filesystem paths; parent,
  child, and similarly named folders do not count as matches.
- Restored and focused the matching Explorer window when found, and opened a
  new window only when no exact match exists.

## 0.11.2

- Moved the normal smooth-surface preparation geometry into the bundled Rust
  engine and parse each mesh only once. On the validated Timer Tower project,
  preparation dropped from about 3 minutes 48 seconds to 29 seconds.
- Kept the protected triangle sets, preparation counts, defaults, and prepared
  project output identical to 0.11.1.
- Retained the Python implementation as a compatibility fallback when the Rust
  engine is unavailable or older expert-only classifiers are requested.

## 0.11.1

- Bundled the compatible Rust G-code engine in the Windows app. The validated
  Timer Tower post-processing pass dropped from about 11 minutes to 84 seconds;
  Bambu Studio slicing remains a separate external cost.
- Added phase timings to the print-ready completion report so slicing and
  FuzzyPaint processing costs can be distinguished directly.
- Added a release self-test that fails if the packaged Rust engine is missing.
- Kept geometry classification, smooth-surface protection, and print settings
  unchanged from 0.11.0.

## 0.11.0

- Added the validated universal smooth-surface protection profile to the GUI:
  large and compact planar patches, disconnected coplanar surface groups, flat
  contacts, and the neighbouring fuzz envelope are protected together when
  **Keep large flat areas smooth** is enabled.
- Changed the default minimum protected flat area to 90 mm². The validated
  compact-strip rule still protects narrow fitting surfaces from 30 mm².
- Changed the older overlap and non-manifold debris checks to opt-in defaults;
  the universal profile does not require them and they add substantial runtime
  on large projects.
- Improved large-model preparation performance by sharing parsed mesh geometry
  between protection passes and avoiding repeated spatial searches.
- Added a packaged-app self-test mode used to verify Windows release builds.

## 0.10.2

- Changed: `_find_coincident_protection` now requires corroborating
  evidence before protecting a coincident pair, not flatness+coincidence
  alone. Of the two shells in a candidate pair, the smaller ("loose
  piece") side must itself contain a non-manifold edge somewhere
  (`_find_nonmanifold_protection`'s signal) -- the fingerprint of
  scarred-in debris, not of ordinary clean decoration. This targets the
  0.10.1 regression directly: the legitimately-loose decorative ledges
  that check wrongly caught are clean, well-formed meshes with no
  non-manifold edges of their own, while the actual mortise-joint debris
  (self-introduced while editing, per the user) is not. Still gated
  behind `--protect-coincident` (off by default) while this narrower rule
  gets validated against the real production model again.

## 0.10.1

- Changed: 0.10.0's coincident-surface check defaulted to on, validated only
  against a hand-picked test crop (one wall + 7 loose stones). Run for
  real against the full ~829,000-triangle production model, it correctly
  fixed the mortise-joint case it was built for, but also removed fuzz
  from several legitimately-touching mechanical parts elsewhere on the
  model (lever arms and ledges resting flat against the tower body --
  confirmed directly in Bambu Studio's sliced preview). A model with this
  many separately-kitbashed functional parts has far more "two flat
  surfaces happen to touch" pairs than a small test piece ever showed, and
  not all of them are defects. Now off by default (`--protect-coincident`
  opt-in, was `--no-protect-coincident` opt-out); the GUI checkbox is
  unchecked by default and labelled experimental. The detection logic
  itself is unchanged -- this is a default-safety change, not a bug fix in
  the check.

## 0.10.0

- Added: a third mesh-debris protection, `_find_coincident_protection`,
  covering a real defect category neither existing check reaches -- two
  disconnected pieces whose flat faces sit almost exactly on top of each
  other (true coincident geometry, the actual cause of z-fighting/flicker
  in a 3D viewer), as opposed to `_find_overlap_protection`'s "crossing at
  an angle" or `_find_nonmanifold_protection`'s "welded into the same
  shell". The real case this was built for: a mortise joint's own flat
  walls, a functional assembly surface never meant to carry rough texture,
  confirmed directly in Bambu Studio's sliced preview. Detection requires
  the two triangles to be in different shells, near-coplanar (within
  `angle_degrees`, default 2.0) and within a tight `max_gap_mm` (default
  0.05 -- true near-zero coincidence, much tighter than the 0.3mm used for
  plain touching). Once found, protection grows to each side's whole
  connected flat patch (the same region-growing used by
  `_find_planar_protection`), not just the seed triangles -- this stays
  naturally bounded, because a flat patch stops growing at a real edge or
  corner, unlike the straight-line radius tried and reverted in 0.9.0.
  Validated against a real exported model section (wall + 7 loose stones):
  6.2% of triangles flagged across 137 separate grown patches, largest
  only 275mm². New `--no-protect-coincident` CLI flag and GUI checkbox,
  default on, matching the existing overlap/non-manifold checks.

## 0.9.0

- Attempted, then reverted to a no-op default: extending the disconnected-
  collision check (0.8.0) to protect deeper into a confirmed-colliding
  chunk, not just the thin first-contact seam. The idea held up on paper
  (real stones measured 2.2-2.5mm embedded, not sub-millimetre, so a
  0.3mm-gap seam alone left most of the intrusion still fuzzed) but a
  straight-line "within depth_mm of the other shell" radius does not
  isolate just the embedded tip on a real kitbashed rock wall -- rocks
  sit close to their own neighbours across most of their own surface, so
  `depth_mm=1.0` already protected 34.6% of a real 30,748-triangle test
  piece, `depth_mm=3.0` protected 76.6%, confirmed by direct visual
  inspection in Bambu Studio's sliced preview as real fuzz loss on
  surfaces that were never part of the defect. `_find_overlap_protection`
  keeps the `depth_mm` parameter and its two-pass structure (a future
  geodesic/surface-distance version could use it correctly), but it now
  defaults to equal `max_gap_mm`, making the second pass a no-op by
  default -- the shipped behaviour is back to exactly 0.8.0's thin-seam
  protection.

## 0.8.2

- Added: GUI checkboxes on the Prepare tab to disable 0.8.0's disconnected-
  collision check and 0.8.1's non-manifold-edge check independently,
  default on. On a large/detailed real model (~829,000 triangles), these
  two checks together added roughly 150 seconds to preparation (confirmed
  going from ~25s to ~170s on the user's actual file) -- a real cost for
  routine iteration, not just a one-off for a final export. Both checks
  were previously only reachable via CLI flags (`--no-protect-overlap`,
  `--no-protect-nonmanifold`); the people actually hitting this cost use
  the GUI, not the CLI.

## 0.8.1

- Added: never apply fuzzy skin to a triangle touching a non-manifold mesh
  edge (an edge shared by 3 or more triangles, instead of the normal 1 or
  2), regardless of paint colour. A different real defect from 0.8.0's
  disconnected-piece check: a small patch of degenerate geometry can be
  genuinely welded to the real surface at a few shared vertices -- found
  on the same real model, in a corner where a colliding chunk (since
  dragged away by the user, the fix for 0.8.0) left behind a scarred
  ridge stitched into the wall itself. Because it shares real vertices
  with the wall, it's the same connected piece by any disconnection test,
  so 0.8.0's check correctly leaves it alone; checking for non-manifold
  edges is a different, well-established mesh-validity signal that
  catches this instead. On by default; disable with
  `--no-protect-nonmanifold` if it's ever not wanted. Verified bounded on
  the same real model (5.6% of all triangles, not a wholesale
  re-flagging); could not be directly verified against the specific
  hand-found cluster from 0.8.0's own verification, because extracting
  that cluster through a Blender round-trip welded together the exact
  duplicated vertex positions this check depends on, destroying the
  signal in that one sample.

## 0.8.0

- Added: never apply fuzzy skin to a triangle that's physically colliding
  with a different, geometrically disconnected piece of the same model,
  regardless of paint colour. Hand-sculpted/kitbashed terrain models are
  often exported as many separate stone/rock chunks that were never welded
  into one watertight mesh; where two chunks end up slightly overlapping
  or crossing into each other, the result is debris sitting on top of real
  paintable surface that no amount of paint cleanup can fix, because the
  slicer may render either layer. Confirmed on a real model: the user
  found, by splitting the mesh into its disconnected pieces in Blender and
  dragging them apart by hand, three small rock chunks slightly crossing
  into a wall that was otherwise correctly protected -- this is exactly
  the category this change targets. On by default (no new setting to
  configure); disable with `--no-protect-overlap` on the command line if
  it's ever not wanted. Two cheaper detection ideas (plain proximity to
  any nearby triangle; requiring the two surfaces to be near-parallel with
  overlapping 2D footprints) were tried and rejected after real-model
  verification showed the first flagged ~98% of all triangles and the
  second still missed most of a known defect. This does not fix every kind
  of mesh debris -- a small patch technically welded to the real surface
  at a few shared vertices is a different, rarer problem that is not
  addressed here.

## 0.7.6

- Fixed: "Keep large flat areas smooth" still left small fuzzy spots inside
  otherwise-flat areas, even at a raised angle tolerance. Root cause: these
  spots are genuine small physical steps in the mesh (e.g. the faint
  outline of a boolean cut that didn't go quite deep enough to remove a
  stone block's shape), not angle-tolerance noise -- no amount of angle
  tuning should or could bridge a real step. Added a second pass that
  absorbs any small leftover patch into protection if it's geometrically
  adjacent to a patch that's already large enough to be protected on its
  own, regardless of its own normal, since a few unfuzzed triangles inside
  an otherwise-flat protected area would look more wrong than just
  smoothing over the whole patch.

## 0.7.5

- Fixed: `reveal_in_explorer()` still opened Documents instead of the
  output folder even after 0.7.3's quoting fix -- confirmed live that the
  corrected command line (verified via `subprocess.list2cmdline`) still
  made `/select,<path>` silently fail on this machine for reasons
  unrelated to quoting. Stopped trying to select the specific file at all
  and just open its folder directly (`explorer <folder>`, no `/select`),
  confirmed working live against the real output path, a user-typed
  arbitrary folder, and the usual `FuzzyPaint Output` folder.

## 0.7.4

- Fixed: text in the main window was selectable but `Ctrl+C` never copied
  it. `SelectableLabel` bound every keypress to unconditionally swallow the
  event (meant to block typing, but the widget's `state="disabled"` already
  did that on its own), which also silently killed Ctrl+C and all other
  read-only keyboard interactions.
- Fixed: error/info/warning dialogs (native `tkinter.messagebox` popups)
  showed text that couldn't even be selected, let alone copied -- the OS's
  own message-box control has no selectable text, and there is no Tk
  option to change that. Replaced every info/error/warning dialog in the
  app with a plain window using the same selectable/copyable text widget
  as the rest of the UI. The short "Delete this preset?" yes/no
  confirmation still uses the native dialog.

## 0.7.3

- Fixed: `reveal_in_explorer()` opened Documents instead of the actual
  output folder after a successful run. Manually wrapping the path in
  literal quotes before handing it to `subprocess.Popen` combined with
  Python's own Windows argument quoting to double-quote it, so
  `explorer.exe` failed to parse the `/select,` argument and silently fell
  back to its default folder. Passing the whole `/select,<path>` as a
  single list item now lets `subprocess`'s own quoting wrap it correctly.

## 0.7.2

- Fixed: "Keep large flat areas smooth" (planar protection) used to compare
  each painted triangle only to its immediate neighbour's normal. Real
  meshes often carry small-scale modelling noise (not fuzz) where two
  adjacent triangles individually differ by more than the angle tolerance
  even though the surface reads as flat -- that local noise could fragment
  one large flat patch into several sub-threshold pieces, each of which
  then got fuzzed despite the whole patch clearly being intended as flat.
  Patches now grow by comparing each candidate triangle to the patch's own
  cumulative area-weighted average normal instead, so noise-level drift
  averages out across the patch while a genuine corner or fold (a real
  normal change) still stops the patch growing through it.

## 0.7.1

- Fixed: suggesting an output path for a file already sitting inside a
  `FuzzyPaint Output` folder (e.g. loading an already-prepped project as
  the next stage's source) nested a second `FuzzyPaint Output` folder
  inside the first, producing an unreachable path and a `WinError 3`
  failure. `suggested_output_path` now reuses that folder instead of
  nesting another one inside it.

## 0.7.0

- Changed: "Create single-filament print file" now runs on a Rust engine
  (`fuzzypaint_engine/`) instead of pure Python, when the extension is
  built -- roughly 7-8x faster on real test files in "both"/"upper"/"lower"
  mode. Output is byte-for-byte identical to the Python implementation on
  everything tested, with two documented, extremely rare exceptions that
  have no print-physical consequence: a handful of G2/G3 arc-derived
  coordinates can differ by 1 floating-point ULP (sin/cos/atan2 aren't
  required to be bit-identical across math libraries), and one cosmetic
  preview-time field in `plate_1.json` can occasionally round to an
  equally-valid but different last digit. Falls back automatically to the
  previous pure-Python path if the Rust extension isn't installed, so
  behavior is unchanged for anyone not building it.

## 0.6.0

- Fixed: expanding "Show advanced settings"/"Show advanced outline settings"
  pushed the primary action button ("Prepare file for slicing" /
  "Create single-filament print file") out of view, since it lived inside
  the same scrollable region as everything else. Each tab's action area
  (status line + button) now docks to a fixed strip at the bottom of the
  window, below a separator, outside the scrollable area entirely -- it no
  longer moves regardless of what's expanded or how far the content above
  is scrolled.
- Replaced the 0.5.0 "overwrite this file?" confirmation with automatic
  "... vN" versioning: if the chosen destination already exists, FuzzyPaint
  now resolves to the next free v2/v3/v4/... name itself (never v1 -- the
  plain name is implicitly that) and updates the destination field to show
  the actual name before the job starts, rather than asking every time.

## 0.5.0

- Fixed a real gap: re-running the single-filament step (or the prepare
  step) on the same input with the same destination filename -- e.g. to try
  a different angle tolerance or outline setting -- silently overwrote the
  previous output with no warning, since the suggested filename doesn't
  auto-number like the prepared-project suggestion does, and there was no
  check in the write path either. Both steps now confirm before overwriting
  an existing destination file.
- Added a tooltip to "Flat-surface angle tolerance" and "Minimum protected
  flat area" explaining what they actually do in plain terms, and when to
  raise the angle tolerance (a painted surface that should be excluded from
  fuzzy skin but isn't, because the underlying mesh isn't flat enough within
  the current tolerance).
- The app now declares itself DPI-aware on Windows before creating its first
  window. An unmanifested Tk app is treated as DPI-unaware, which Windows
  compensates for by bitmap-scaling and sometimes repositioning the window,
  especially across monitors with different scaling factors -- a likely
  contributor to a reported "window jumped to the other monitor after
  loading" (the window-height explanation in 0.4.1 may not be the whole
  story; this is a more direct fix for the actual Windows-level mechanism).

## 0.4.1

- Fixed: `SelectableLabel` called `update_idletasks()` (a full-window layout
  recompute) for every single instance, even ones that obviously fit on one
  line. With ~66 of these now on screen, that showed up as the whole UI
  visibly "building itself" piece by piece on startup. Now skips that
  expensive measurement entirely unless the text could plausibly wrap.
- Fixed: the window no longer relies on Windows' default placement heuristic
  (which could relocate it to a second monitor once the taller window no
  longer fit the first), and instead explicitly centres it on the primary
  monitor.

## 0.4.0

- Fixed a real correctness bug in the "keep large flat areas smooth" fuzz-
  exclusion heuristic: it matched triangle adjacency by raw vertex index, so
  a duplicated vertex at a UV/hard-normal seam (routine on models with baked
  texture-like geometry) could silently fragment one large flat patch into
  several small ones, each falling under the minimum-area threshold and
  getting fuzzed even though the whole patch should have been protected.
  Adjacency is now matched by vertex position (rounded to tolerate float
  noise), so patches split only by duplicate seam vertices merge correctly.
- After "Create single-filament print file" finishes, FuzzyPaint now opens
  (or brings forward) the output file's folder in Explorer with the file
  selected, instead of leaving you to navigate there yourself.
- Output filenames now use FuzzyPaint's own suffixes instead of the old
  Colour2Fuzz ones: `(C2F-Prepped)` → `(FP-Prepped)`, `(C2F-Created)` →
  `(FP-Created)`, `(C2F-Print)` → `(FP-Print_Ready)`. Re-running FuzzyPaint on
  a file still named with an old `(C2F-...)` suffix still strips it correctly
  rather than appending a second suffix.

## 0.3.1

- Fixed: both tab content frames were double-padded (the notebook tab frame
  and the scrollable content frame inside it each added 16px on every side),
  wasting ~32px of vertical space and forcing a scroll to reach the primary
  action button even with advanced settings collapsed. Removed the outer
  duplicate and made the default window a bit taller.
- "Show advanced settings" / "Show advanced outline settings" are now a bold,
  arrow-prefixed disclosure toggle (▸/▾) instead of a checkbox, to read as a
  section expander rather than another setting to leave checked.

## 0.3.0

- Output destinations in the GUI now default to a `FuzzyPaint Output` subfolder
  of the input file's own folder, instead of sitting alongside it.
- New toolbar: a Preset dropdown (Save Preset / Delete Preset), Save Settings
  (remembers current choices for next launch), Reset to Defaults, and a View on
  GitHub link. Settings and presets persist as JSON under `%APPDATA%\FuzzyPaint`.
- Tab 2's retraction/wipe/Z-hop checkboxes are enabled by default and tidied to
  share one "recommended for this workflow" note instead of repeating it three
  times; tab 1's intro text was shortened with the detail moved into a tooltip.
- About window: text renders through lightweight `#`/`##` Markdown-style
  headings instead of an unstyled ALL-CAPS line, fonts are larger throughout,
  and clicking "About / Help" repeatedly now raises the existing window instead
  of opening duplicates.
- Fixed: tooltips could never be read by moving the mouse into them (the
  source widget's `<Leave>` destroyed the tooltip first) -- they now use a
  short hide grace period and track hover on the popup itself.
- Fixed: tooltip and essentially all static/status/caption text throughout the
  app is now mouse-selectable and Ctrl+C copyable (a new `SelectableLabel`
  widget, plus a composite checkbox+caption helper for checkbox text, which
  `ttk.Checkbutton` never allowed to be copyable on its own). Button captions
  are intentionally left native.
- Fixed: `SelectableLabel` was using Tk's plain default font instead of ttk's
  themed label font, visible as a font change in tooltips.
- Project restructured into its own private GitHub repo
  (`Woogie-Maker/FuzzyPaint`), renamed throughout from Colour2Fuzz, with
  generated/experimental `.3mf`/`.gcode` test artifacts moved out of the repo
  entirely rather than merely gitignored.

## 0.2.6 and earlier

Not tracked in this file; see the project's prior history in ChatGPT/Codex
sessions for the paint-to-fuzz converter, the single-filament collapse step,
and the cross-layer inset G-code post-processor.
