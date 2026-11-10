# Changelog

Version numbers follow [Semantic Versioning](https://semver.org/): `MAJOR.MINOR.PATCH`.

- **MAJOR** — incompatible changes (e.g. a file format change, a removed workflow).
- **MINOR** — new backward-compatible functionality (a new toolbar, a new setting).
- **PATCH** — backward-compatible bug fixes only.

The version bump happens in the same commit as the change it describes, not as a
separate afterthought -- that's what let `APP_VERSION` drift and sit at 0.2.6
through several real changes before this file existed.

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
