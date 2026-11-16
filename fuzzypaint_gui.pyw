"""Two-stage Tkinter front end for FuzzyPaint."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from fuzzypaint import (
    ConversionError,
    clean_sliced_output_base,
    configure_project_for_slicing,
    convert_3mf,
    format_report,
    next_available_destination,
    parse_slots,
    suggest_prepared_project_name,
    suggest_print_package_name,
)
from gcode_layer_interval_detour import build_interval_detours
from gcode_single_filament import CollapseReport, collapse_gcode_3mf
from bambu_cli import archive_has_gcode, slice_project
from explorer_reuse import reveal_in_explorer

try:
    import fuzzypaint_engine
except ImportError:
    fuzzypaint_engine = None
else:
    # A source checkout also contains the Rust crate directory, which Python
    # can see as an empty namespace package even when no compatible compiled
    # extension is installed. Only advertise the engine when the operation
    # used by this GUI is genuinely available.
    if not hasattr(fuzzypaint_engine, "process_gcode_3mf"):
        fuzzypaint_engine = None


APP_VERSION = "0.12.0"
SETTINGS_SCHEMA_VERSION = 2

OUTPUT_SUBFOLDER_NAME = "FuzzyPaint Output"
GITHUB_URL = "https://github.com/Woogie-Maker/FuzzyPaint"

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "FuzzyPaint"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
PRESETS_FILE = CONFIG_DIR / "presets.json"


def suggested_output_path(source: Path, filename: str) -> Path:
    """Place a suggested output file in a FuzzyPaint Output subfolder next to the source.

    If the source is already inside a FuzzyPaint Output folder (e.g. it was
    loaded as the next stage's input straight from the previous stage's
    output), use that same folder instead of nesting another one inside it.
    """
    if source.parent.name == OUTPUT_SUBFOLDER_NAME:
        return source.parent / filename
    return source.parent / OUTPUT_SUBFOLDER_NAME / filename


def _load_json_dict(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_json_dict(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _insert_formatted(text_widget: tk.Text, source: str) -> None:
    """Render a small Markdown-like subset (#, ##, blank-line paragraphs) into a Text widget."""
    text_widget.tag_configure("h1", font=("Segoe UI", 16, "bold"), spacing1=12, spacing3=6)
    text_widget.tag_configure("h2", font=("Segoe UI", 13, "bold"), spacing1=10, spacing3=4)
    text_widget.tag_configure("body", font=("Segoe UI", 12), spacing3=10)
    for block in source.strip("\n").split("\n\n"):
        block = block.strip("\n")
        if not block:
            continue
        if block.startswith("## "):
            text_widget.insert("end", block[3:].strip() + "\n", "h2")
        elif block.startswith("# "):
            text_widget.insert("end", block[2:].strip() + "\n", "h1")
        else:
            text_widget.insert("end", block + "\n\n", "body")


PRESETS = {
    "Balanced — proven settings": (0.24, 0.10, 0.55, 0.21, 0.10, 1.20, 0.00),
    "Fine detail — slower": (0.24, 0.05, 0.45, 0.21, 0.05, 1.20, 0.00),
    "Fast — lower resolution": (0.24, 0.20, 0.65, 0.25, 0.20, 1.20, 0.00),
    "Custom": None,
}


class SelectableLabel(tk.Text):
    """A read-only, mouse-selectable/copyable Text widget that looks and wraps like a Label.

    Disabling a Text widget blocks typing but not mouse selection or <<Copy>>,
    so this gives Label-like static or textvariable-bound text that the user
    can drag-select and Ctrl+C -- plain ttk.Label never allows that.
    """

    def __init__(
        self,
        parent,
        *,
        text: str = "",
        textvariable: tk.StringVar | None = None,
        font=None,
        foreground: str | None = None,
        justify: str = "left",
        **kwargs,
    ) -> None:
        style = ttk.Style()
        background = style.lookup("TLabel", "background") or "SystemButtonFace"
        kwargs.setdefault("cursor", "xterm")
        super().__init__(
            parent,
            wrap="word",
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            background=background,
            padx=0,
            pady=0,
            height=1,
            takefocus=False,
            **kwargs,
        )
        # Tk's Text widget doesn't pick up the ttk theme's label font on its own,
        # so match it explicitly unless the caller asked for something specific.
        self.configure(font=font or style.lookup("TLabel", "font") or "TkDefaultFont")
        self._foreground = foreground or ""
        self.tag_configure("body", justify=justify, foreground=self._foreground)
        # state="disabled" (set in _set_text) already blocks typing on its
        # own; a blanket `<Key>` -> "break" binding here used to also kill
        # Ctrl+C and every other read-only keyboard interaction (arrow-key
        # navigation, Ctrl+A), directly undermining the "mouse selection
        # or <<Copy>>" promise this class's own docstring makes.
        self.bind("<Configure>", self._resize, add="+")
        self._textvariable = textvariable
        if textvariable is not None:
            textvariable.trace_add("write", self._on_variable_changed)
            self._set_text(textvariable.get())
        else:
            self._set_text(text)

    def _on_variable_changed(self, *_args) -> None:
        self._set_text(self._textvariable.get())

    def _set_text(self, value: str) -> None:
        self.configure(state="normal")
        self.delete("1.0", "end")
        self.insert("1.0", value, "body")
        self.configure(state="disabled")
        self._resize()

    def _resize(self, _event=None) -> None:
        # update_idletasks() forces Tk to recompute layout for the whole
        # window, not just this widget -- fine occasionally, but with dozens
        # of these on screen it turns startup into dozens of full reflows
        # (visible as the UI "building itself" piece by piece). Most captions
        # obviously fit on one line without ever measuring that, so only pay
        # for the expensive, accurate measurement when wrapping is possible.
        value = self.get("1.0", "end-1c")
        width = int(self.cget("width") or 0)
        if "\n" not in value and (width <= 0 or len(value) <= width):
            if int(self.cget("height")) != 1:
                self.configure(height=1)
            return
        self.update_idletasks()
        lines = self.count("1.0", "end", "displaylines")
        height = max(1, lines[0] if lines else 1)
        if int(self.cget("height")) != height:
            self.configure(height=height)

    def set_enabled(self, enabled: bool) -> None:
        """Dim/undim this label's text to mirror a ttk widget's disabled look.

        The widget itself always stays Tk-state "disabled" (that's what makes
        it read-only-but-selectable); this only changes its foreground colour
        so it visually greys out alongside real widgets in the same panel.
        """
        self.tag_configure("body", foreground=self._foreground if enabled else "#999999")


def show_message(parent, title: str, message: str) -> None:
    """Modal message dialog with selectable/copyable text.

    `tkinter.messagebox`'s native dialogs render their text with the OS's
    own message-box control, whose text cannot be selected at all (not even
    read-only-selectable like SelectableLabel) -- there is no Tk option to
    change that. This replaces it with a plain Toplevel + SelectableLabel
    for every info/error/warning dialog in the app, so the report text,
    error messages, and settings confirmations can all be copied.
    """
    window = tk.Toplevel(parent)
    window.title(title)
    window.transient(parent)
    window.resizable(False, False)
    body = ttk.Frame(window, padding=18)
    body.pack(fill="both", expand=True)
    SelectableLabel(body, text=message, width=64).pack(fill="both", expand=True, pady=(0, 14))
    button_row = ttk.Frame(body)
    button_row.pack(fill="x")
    ok_button = ttk.Button(button_row, text="OK", command=window.destroy)
    ok_button.pack(side="right")
    window.bind("<Return>", lambda _event: window.destroy())
    window.bind("<Escape>", lambda _event: window.destroy())
    window.protocol("WM_DELETE_WINDOW", window.destroy)
    window.update_idletasks()
    window.geometry(f"+{parent.winfo_rootx() + 40}+{parent.winfo_rooty() + 40}")
    window.grab_set()
    ok_button.focus_set()
    parent.wait_window(window)


class ToolTip:
    """Delayed hover help that stays open -- and selectable -- while the

    pointer is over either the source widget or the popup itself. Hiding is
    deferred by a short grace period so moving the mouse from the source
    widget into the popup (to select/copy its text) doesn't close it first.
    """

    def __init__(self, widget, text: str) -> None:
        self.widget = widget
        self.text = text
        self.show_after_id = None
        self.hide_after_id = None
        self.window = None
        widget.bind("<Enter>", self._schedule_show, add="+")
        widget.bind("<Leave>", self._schedule_hide, add="+")
        widget.bind("<ButtonPress>", self._hide_now, add="+")
        widget._fuzzypaint_tooltip = self

    def _schedule_show(self, _event=None) -> None:
        self._cancel_hide()
        self._cancel_show()
        self.show_after_id = self.widget.after(450, self._show)

    def _cancel_show(self) -> None:
        if self.show_after_id is not None:
            self.widget.after_cancel(self.show_after_id)
            self.show_after_id = None

    def _schedule_hide(self, _event=None) -> None:
        self._cancel_show()
        self._cancel_hide()
        self.hide_after_id = self.widget.after(200, self._hide_now)

    def _cancel_hide(self) -> None:
        if self.hide_after_id is not None:
            self.widget.after_cancel(self.hide_after_id)
            self.hide_after_id = None

    def _show(self) -> None:
        self.show_after_id = None
        if self.window is not None:
            return
        background = ttk.Style().lookup("TLabel", "background") or "SystemButtonFace"
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(
            f"+{self.widget.winfo_rootx() + 18}+{self.widget.winfo_rooty() + 22}"
        )
        border = tk.Frame(self.window, background=background, relief="solid", borderwidth=1)
        border.pack()
        SelectableLabel(border, text=self.text, width=46).pack(padx=9, pady=6)
        self.window.bind("<Enter>", lambda _event: self._cancel_hide(), add="+")
        self.window.bind("<Leave>", self._schedule_hide, add="+")

    def _hide_now(self, _event=None) -> None:
        self._cancel_show()
        self._cancel_hide()
        if self.window is not None:
            self.window.destroy()
            self.window = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # Build the complete interface off-screen. SelectableLabel performs
        # geometry-dependent sizing during construction; exposing the root
        # window first lets Windows paint each intermediate layout and makes
        # startup look substantially slower than it is.
        self.withdraw()
        self.title("FuzzyPaint")
        self.minsize(800, 680)
        # Pin an explicit, centred position on the primary monitor instead of
        # leaving placement to Windows' own default-window-position heuristic,
        # which can otherwise land the window on a different monitor than
        # expected once its requested size no longer fits the first one.
        width, height = 900, 860
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 3)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.job_queue: queue.Queue | None = None
        self.job_button: ttk.Button | None = None
        self.job_done = None

        outer = ttk.Frame(self, padding=(18, 14))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x")
        SelectableLabel(header, text="FuzzyPaint", font=("Segoe UI", 18, "bold"), width=12).pack(side="left")
        ttk.Button(header, text="About / Help", command=self.show_help).pack(side="right")
        SelectableLabel(
            outer,
            text="for Bambu Studio — Turn painted colours into surface texture and single-filament detail.",
            width=90,
        ).pack(anchor="w", pady=(2, 10))

        toolbar = ttk.Frame(outer)
        toolbar.pack(fill="x", pady=(0, 10))
        SelectableLabel(toolbar, text="Preset:", width=8).pack(side="left")
        self.preset_choice = tk.StringVar()
        self.preset_combo = ttk.Combobox(
            toolbar, textvariable=self.preset_choice, state="readonly", width=24
        )
        self.preset_combo.pack(side="left", padx=(6, 6))
        self.preset_combo.bind("<<ComboboxSelected>>", self.apply_selected_preset)
        ttk.Button(toolbar, text="Save Preset…", command=self.save_preset).pack(side="left", padx=(0, 4))
        ttk.Button(toolbar, text="Delete Preset", command=self.delete_selected_preset).pack(
            side="left", padx=(0, 12)
        )
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="Save Settings", command=self.save_settings).pack(side="left", padx=(6, 4))
        ttk.Button(toolbar, text="Reset to Defaults", command=self.reset_to_defaults).pack(side="left")
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="View on GitHub", command=self.open_github).pack(side="left")

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)
        self.prepare_tab = ttk.Frame(self.notebook)
        self.gcode_tab = ttk.Frame(self.notebook)
        self.notebook.add(self.prepare_tab, text="1  Prepare project")
        self.notebook.add(self.gcode_tab, text="2  Create single-filament file")
        self._build_prepare_tab()
        self._build_gcode_tab()

        self._default_settings = self._collect_settings()
        self._load_settings_file()
        self._refresh_preset_list()

        footer = ttk.Frame(outer)
        footer.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(footer, mode="indeterminate", length=170)
        self.progress.pack(side="right")
        SelectableLabel(
            footer,
            text="Source files are never overwritten. Inspect the complete Bambu Studio Preview before printing.",
            foreground="#555555",
            width=90,
        ).pack(side="left")

        # Resolve all pending geometry while hidden, then present one complete
        # frame instead of visibly assembling the UI widget by widget.
        self.update_idletasks()
        self.deiconify()

    @staticmethod
    def _avoid_overwrite(destination: Path, destination_variable: tk.StringVar) -> Path:
        """Auto-resolve a destination that already exists to the next "... vN" name.

        Re-running on the same input with the same destination -- for
        example, to try a different setting -- would otherwise silently
        replace the previous output, since the suggested filename is the
        same every time (it doesn't auto-number like the prepared-project
        suggestion does). Resolving automatically, rather than popping an
        "overwrite?" dialog on every rerun, keeps every previous attempt on
        disk; the destination field is updated so the actual output name
        that will be written stays visible before the job even starts.
        """
        resolved = next_available_destination(destination)
        if resolved != destination:
            destination_variable.set(str(resolved))
        return resolved

    @staticmethod
    def _file_row(parent, row, label, variable, command) -> None:
        SelectableLabel(parent, text=label, width=24).grid(row=row, column=0, sticky="w", pady=7)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=7)
        ttk.Button(parent, text="Browse…", command=command).grid(row=row, column=2, sticky="ew", pady=7)

    @staticmethod
    def _number_row(parent, row, label, variable, units, hint="", tooltip: str | None = None) -> None:
        name_label = SelectableLabel(parent, text=label, width=28)
        name_label.grid(row=row, column=0, sticky="w", pady=5)
        if tooltip:
            ToolTip(name_label, tooltip)
        ttk.Entry(parent, textvariable=variable, width=12).grid(row=row, column=1, sticky="w", padx=(12, 6), pady=5)
        SelectableLabel(
            parent, text=f"{units}  {hint}".rstrip(), foreground="#555555", width=22
        ).grid(row=row, column=2, sticky="w", pady=5)

    def _scrollable_content(self, host, tab) -> ttk.Frame:
        """Build a scrollable area inside `host`, which is scrolled only
        while `tab` (the actual notebook tab page) is the selected one.

        `host` is a plain sub-frame of `tab`, not the whole tab page -- that
        split is what lets a fixed action-button area dock below the scroll
        area instead of scrolling away with everything else.
        """
        canvas = tk.Canvas(host, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(host, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, padding=16)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")

        def resize_content(event):
            canvas.itemconfigure(window_id, width=event.width)

        def update_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def wheel(event):
            # bind_all is needed so controls embedded in the canvas can scroll it,
            # but events from secondary windows (notably About / Help) must not
            # leak through and move the main form underneath them.
            try:
                if event.widget.winfo_toplevel() is not self:
                    return None
            except tk.TclError:
                return None
            if self.notebook.select() == str(tab):
                canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
                return "break"
            return None

        canvas.bind("<Configure>", resize_content)
        content.bind("<Configure>", update_region)
        self.bind_all("<MouseWheel>", wheel, add="+")
        return content

    @staticmethod
    def _text_setting_row(parent, row, label, variable) -> None:
        SelectableLabel(parent, text=label, width=22).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=variable, width=18).grid(row=row, column=1, sticky="w", padx=(12, 6), pady=5)

    @staticmethod
    def _labeled_check(parent, text, variable, command=None) -> ttk.Frame:
        """A Checkbutton with its caption as a separate, copyable SelectableLabel.

        ttk.Checkbutton's own `text` is drawn by the widget itself and can
        never be selected/copied, so the caption lives beside it instead;
        clicking the caption still toggles the checkbox like a normal label.
        """
        row = ttk.Frame(parent)
        check = ttk.Checkbutton(row, variable=variable, command=command)
        check.pack(side="left")
        label = SelectableLabel(row, text=text, width=max(20, len(text)))
        label.pack(side="left")

        def toggle(_event=None) -> None:
            variable.set(not variable.get())
            if command:
                command()

        label.bind("<Button-1>", toggle)
        return row

    @staticmethod
    def _disclosure_toggle(parent, text, variable: tk.BooleanVar, command=None) -> SelectableLabel:
        """A bold, arrow-prefixed label that expands/collapses a section on click.

        Used instead of a checkbox for "Show advanced ..." -- the arrow makes
        it read as a section disclosure rather than another setting to leave
        checked, and it stands out more than a same-weight checkbox caption.
        """
        display = tk.StringVar()

        def refresh(*_args) -> None:
            display.set(("▾ " if variable.get() else "▸ ") + text)

        variable.trace_add("write", refresh)
        refresh()
        label = SelectableLabel(
            parent, textvariable=display, font=("Segoe UI", 10, "bold"), cursor="hand2"
        )

        def toggle(_event=None) -> None:
            variable.set(not variable.get())
            if command:
                command()

        label.bind("<Button-1>", toggle)
        return label

    def _build_prepare_tab(self) -> None:
        action_host = ttk.Frame(self.prepare_tab, padding=(16, 8, 16, 14))
        action_host.pack(side="bottom", fill="x")
        ttk.Separator(self.prepare_tab, orient="horizontal").pack(side="bottom", fill="x")
        scroll_host = ttk.Frame(self.prepare_tab)
        scroll_host.pack(side="top", fill="both", expand=True)

        tab = self._scrollable_content(scroll_host, self.prepare_tab)
        tab.columnconfigure(0, weight=1)
        intro = ttk.Frame(tab)
        intro.grid(row=0, column=0, sticky="w", pady=(0, 10))
        SelectableLabel(
            intro, text="Prepare the painted project for Fuzzy multicolour slicing.", width=60
        ).pack(side="left")
        intro_help = SelectableLabel(intro, text="  ?", width=2, cursor="question_arrow", foreground="#555555")
        intro_help.pack(side="left")
        ToolTip(intro_help, "A new .3mf file will be created; the original file is left untouched.")

        self.project_source = tk.StringVar()
        self.project_destination = tk.StringVar()
        self.project_slots = tk.StringVar(value="2")
        self.protect_planar = tk.BooleanVar(value=True)
        self.protect_overlap = tk.BooleanVar(value=False)
        self.protect_nonmanifold = tk.BooleanVar(value=False)
        self.protect_coincident = tk.BooleanVar(value=False)
        self.disable_wipe = tk.BooleanVar(value=True)
        self.disable_retractions = tk.BooleanVar(value=True)
        self.force_sloped_z_hop = tk.BooleanVar(value=True)
        self.strip_prepare_attachments = tk.BooleanVar(value=True)
        self.prepare_status = tk.StringVar(value="Start by choosing your colour-painted Bambu Studio project.")
        self.prepare_advanced = tk.BooleanVar(value=False)
        self.planar_angle = tk.StringVar(value="2.0")
        self.planar_area = tk.StringVar(value="90.0")
        self.override_fuzzy = tk.BooleanVar(value=True)
        self.fuzzy_thickness = tk.StringVar(value="0.25")
        self.fuzzy_distance = tk.StringVar(value="0.05")
        self.fuzzy_first_layer = tk.BooleanVar(value=False)
        self.fuzzy_mode = tk.StringVar(value="displacement")
        self.fuzzy_noise = tk.StringVar(value="classic")
        self.fuzzy_octaves = tk.StringVar(value="4")
        self.fuzzy_persistence = tk.StringVar(value="0.5")
        self.fuzzy_scale = tk.StringVar(value="1.0")

        form = ttk.LabelFrame(tab, text="Project", padding=12)
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self._file_row(form, 0, "Colour-painted project", self.project_source, self.choose_project_source)
        self._file_row(form, 1, "Save prepared copy as", self.project_destination, self.choose_project_destination)
        fuzz_label = ttk.Frame(form)
        fuzz_label.grid(row=2, column=0, sticky="w", pady=7)
        SelectableLabel(fuzz_label, text="Add fuzz to painted colour(s)", width=30).pack(side="left")
        help_icon = SelectableLabel(fuzz_label, text="  ?", width=2, cursor="question_arrow", foreground="#555555")
        help_icon.pack(side="left")
        ToolTip(
            help_icon,
            "Enter the Bambu filament number used by face painting, such as 2 or 2,4. "
            "Only colour-painted faces become fuzzy. Separate parts assigned to the same "
            "filament—such as the vine—are left unchanged.",
        )
        ttk.Entry(form, textvariable=self.project_slots).grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=7)
        self._labeled_check(
            form, "Keep large flat areas smooth (recommended for slots and fitting surfaces)", self.protect_planar
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(7, 2))
        self._labeled_check(
            form, "Disable retractions (recommended for this single-filament workflow)", self.disable_retractions
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(4, 2))
        self._labeled_check(
            form,
            "Disable wipe while retracting (recommended for this single-filament workflow)",
            self.disable_wipe,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 2))
        self._labeled_check(
            form, "Use sloped Z-hop for every filament (recommended)", self.force_sloped_z_hop
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(4, 2))
        self._labeled_check(
            form,
            "Remove embedded guides and downloads from the prepared copy (faster)",
            self.strip_prepare_attachments,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(4, 2))
        overlap_check = self._labeled_check(
            form,
            "Skip fuzzing mesh debris that collides with a disconnected part (slower on large models)",
            self.protect_overlap,
        )
        overlap_check.grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 2))
        ToolTip(
            overlap_check,
            "Detects stray, never-welded chunks physically overlapping another part of the "
            "model and never fuzzes them, regardless of paint. On a very large/detailed model "
            "(e.g. hundreds of thousands of triangles) this can add a couple of minutes to "
            "preparation. Turn off for quick iteration; turn on for your final output.",
        )
        nonmanifold_check = self._labeled_check(
            form,
            "Skip fuzzing mesh debris stitched onto a non-manifold edge (slower on large models)",
            self.protect_nonmanifold,
        )
        nonmanifold_check.grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 2))
        ToolTip(
            nonmanifold_check,
            "Detects triangles touching an edge shared by 3+ triangles (a sign of debris "
            "welded onto the real surface) and never fuzzes them, regardless of paint. Adds "
            "some preparation time on very large/detailed models. Turn off for quick "
            "iteration; turn on for your final output.",
        )
        coincident_check = self._labeled_check(
            form,
            "Skip fuzzing flat surfaces that sit coincident with another part (experimental, off by default)",
            self.protect_coincident,
        )
        coincident_check.grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 2))
        ToolTip(
            coincident_check,
            "Detects two disconnected parts' flat faces sitting almost exactly on top of each "
            "other (the real cause of z-fighting/flicker in a 3D viewer) and never fuzzes "
            "either one, for the whole flat surface each belongs to -- regardless of paint. "
            "Validated well on a hand-picked mortise-joint test case, but on a complex model "
            "with many legitimately-touching mechanical parts (levers, ledges, hinges) it can "
            "also remove fuzz from surfaces that were never a defect. Off by default -- turn "
            "on and check the sliced result carefully before trusting it for a final export.",
        )

        self._disclosure_toggle(
            tab, "Show advanced settings", self.prepare_advanced, command=self._toggle_prepare_advanced
        ).grid(row=2, column=0, sticky="w", pady=(10, 2))
        self.prepare_advanced_frame = ttk.LabelFrame(tab, text="Advanced settings", padding=12)
        self._number_row(
            self.prepare_advanced_frame,
            0,
            "Flat-surface angle tolerance",
            self.planar_angle,
            "degrees",
            tooltip=(
                "Fuzzy skin is withheld from a painted area only where it is flat enough: "
                "adjacent surface normals must not differ by more than this many degrees. "
                "A painted surface that is less flat than this tolerance still gets fuzzy "
                "skin, even with “Keep large flat areas smooth” enabled. Raise this "
                "value if a surface you expect to be excluded (such as a poorly modelled flat "
                "mortise or mating face) is still getting fuzzed."
            ),
        )
        self._number_row(
            self.prepare_advanced_frame,
            1,
            "Minimum protected flat area",
            self.planar_area,
            "mm²",
            tooltip=(
                "Among the flat-enough regions found above, only ones at least this large "
                "are withheld from fuzzy skin. Smaller flat patches still get fuzzed even if "
                "they pass the angle tolerance."
            ),
        )
        self._labeled_check(
            self.prepare_advanced_frame,
            "Override fuzzy-skin process settings stored in the output project",
            self.override_fuzzy,
            command=self._toggle_fuzzy_settings,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 3))
        self.fuzzy_frame = ttk.Frame(self.prepare_advanced_frame)
        self.fuzzy_frame.grid(row=3, column=0, columnspan=3, sticky="ew")
        self._number_row(self.fuzzy_frame, 0, "Thickness", self.fuzzy_thickness, "mm")
        self._number_row(self.fuzzy_frame, 1, "Point distance", self.fuzzy_distance, "mm")
        self._labeled_check(self.fuzzy_frame, "Fuzzy skin on first layer", self.fuzzy_first_layer).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=4
        )
        self._text_setting_row(self.fuzzy_frame, 3, "Mode", self.fuzzy_mode)
        self._text_setting_row(self.fuzzy_frame, 4, "Noise type", self.fuzzy_noise)
        self._number_row(self.fuzzy_frame, 5, "Noise octaves", self.fuzzy_octaves, "")
        self._number_row(self.fuzzy_frame, 6, "Noise persistence", self.fuzzy_persistence, "")
        self._number_row(self.fuzzy_frame, 7, "Noise scale", self.fuzzy_scale, "")
        self._toggle_fuzzy_settings()
        self._toggle_prepare_advanced()

        action_host.columnconfigure(0, weight=1)
        SelectableLabel(action_host, textvariable=self.prepare_status, width=92).grid(
            row=0, column=0, columnspan=2, sticky="ew"
        )
        self.prepare_button = ttk.Button(
            action_host, text="Prepare file for slicing", command=self.prepare_project
        )
        self.prepare_button.grid(row=1, column=1, sticky="e", pady=(8, 0))

    def _build_gcode_tab(self) -> None:
        action_host = ttk.Frame(self.gcode_tab, padding=(16, 8, 16, 14))
        action_host.pack(side="bottom", fill="x")
        ttk.Separator(self.gcode_tab, orient="horizontal").pack(side="bottom", fill="x")
        scroll_host = ttk.Frame(self.gcode_tab)
        scroll_host.pack(side="top", fill="both", expand=True)

        tab = self._scrollable_content(scroll_host, self.gcode_tab)
        tab.columnconfigure(0, weight=1)
        SelectableLabel(
            tab,
            text=(
                "Choose the prepared FuzzyPaint project. The tool will use Bambu Studio's slicer automatically, "
                "then keep the painted outlines while changing the result to one spool—without colour changes "
                "or a purge tower. You can also choose a plate sliced file you exported manually."
            ),
            width=95,
        ).grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.gcode_source = tk.StringVar()
        self.gcode_destination = tk.StringVar()
        self.gcode_slots = tk.StringVar(value="2")
        self.gcode_preset = tk.StringVar(value="Balanced — proven settings")
        self.detour_side = tk.StringVar(value="Both — complete outline")
        self.gcode_status = tk.StringVar(value="Choose a prepared project or an exported plate sliced file.")
        self.gcode_advanced = tk.BooleanVar(value=False)
        self.detour_depth = tk.StringVar()
        self.boundary_resolution = tk.StringVar()
        self.transition_distance = tk.StringVar()
        self.endpoint_coverage = tk.StringVar()
        self.minimum_span = tk.StringVar()
        self.inner_search = tk.StringVar()
        self.same_layer_gap = tk.StringVar()
        self.filament_overrides = tk.StringVar()
        self.strip_attachments = tk.BooleanVar(value=True)
        self._apply_preset()

        SelectableLabel(tab, textvariable=self.gcode_status, width=95).grid(
            row=1, column=0, sticky="ew", pady=(0, 10)
        )

        form = ttk.LabelFrame(tab, text="Prepared project or sliced package", padding=12)
        form.grid(row=2, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self._file_row(form, 0, "Prepared project / sliced file", self.gcode_source, self.choose_gcode_source)
        self._file_row(form, 1, "Save finished print file as", self.gcode_destination, self.choose_gcode_destination)
        SelectableLabel(form, text="Painted colour slot(s) to outline", width=32).grid(
            row=2, column=0, sticky="w", pady=7
        )
        ttk.Entry(form, textvariable=self.gcode_slots).grid(row=2, column=1, sticky="ew", padx=(12, 8), pady=7)
        SelectableLabel(form, text="Use Bambu's number: 2 or 2,4", foreground="#555555", width=26).grid(
            row=2, column=2, sticky="w"
        )
        SelectableLabel(form, text="Preset", width=10).grid(row=3, column=0, sticky="w", pady=7)
        preset = ttk.Combobox(form, textvariable=self.gcode_preset, state="readonly", values=tuple(PRESETS))
        preset.grid(row=3, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=7)
        preset.bind("<<ComboboxSelected>>", lambda _event: self._apply_preset())
        SelectableLabel(form, text="Outline direction", width=18).grid(row=4, column=0, sticky="w", pady=7)
        ttk.Combobox(
            form,
            textvariable=self.detour_side,
            state="readonly",
            values=("Both — complete outline", "Above painted regions", "Below painted regions"),
        ).grid(row=4, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=7)
        self._labeled_check(
            form,
            "Remove embedded guides, pictures and downloads (recommended for faster transfer)",
            self.strip_attachments,
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(7, 2))
        SelectableLabel(
            form,
            text="All three below are enabled by default and recommended for this workflow:",
            foreground="#555555",
            width=70,
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 0))
        self._labeled_check(
            form, "Disable retractions before automatic slicing", self.disable_retractions
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(2, 2))
        self._labeled_check(
            form, "Disable wipe while retracting before automatic slicing", self.disable_wipe
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 2))
        self._labeled_check(
            form, "Use sloped Z-hop before automatic slicing", self.force_sloped_z_hop
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(4, 2))

        self._disclosure_toggle(
            tab, "Show advanced outline settings", self.gcode_advanced, command=self._toggle_gcode_advanced
        ).grid(row=3, column=0, sticky="w", pady=(10, 2))
        self.gcode_advanced_frame = ttk.LabelFrame(tab, text="Advanced outline settings", padding=12)
        self._number_row(self.gcode_advanced_frame, 0, "Detour depth", self.detour_depth, "mm", "0.24 proven")
        self._number_row(self.gcode_advanced_frame, 1, "Detection resolution", self.boundary_resolution, "mm", "smaller = slower")
        self._number_row(self.gcode_advanced_frame, 2, "Transition distance", self.transition_distance, "mm")
        self._number_row(self.gcode_advanced_frame, 3, "Endpoint coverage", self.endpoint_coverage, "mm")
        self._number_row(self.gcode_advanced_frame, 4, "Minimum detected span", self.minimum_span, "mm")
        self._number_row(self.gcode_advanced_frame, 5, "Inner-wall search distance", self.inner_search, "mm")
        self._number_row(self.gcode_advanced_frame, 6, "Same-layer colour gap", self.same_layer_gap, "mm", "0 disables it")
        SelectableLabel(self.gcode_advanced_frame, text="Paint correction ranges", width=22).grid(
            row=7, column=0, sticky="w", pady=5
        )
        ttk.Entry(self.gcode_advanced_frame, textvariable=self.filament_overrides).grid(
            row=7, column=1, sticky="ew", padx=(12, 6), pady=5
        )
        SelectableLabel(
            self.gcode_advanced_frame,
            text="optional: 61428-61485:2, …",
            foreground="#555555",
            width=24,
        ).grid(row=7, column=2, sticky="w", pady=5)
        self.gcode_advanced_frame.columnconfigure(1, weight=1)
        self._toggle_gcode_advanced()

        action_host.columnconfigure(0, weight=1)
        SelectableLabel(
            action_host,
            text=(
                "After creation: open the finished .gcode.3mf in Bambu Studio and inspect the complete "
                "Preview before sending it to the printer."
            ),
            width=95,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.gcode_button = ttk.Button(
            action_host, text="Create single-filament print file", command=self.process_gcode
        )
        self.gcode_button.grid(row=1, column=1, sticky="e", pady=(10, 0))

    def _toggle_prepare_advanced(self) -> None:
        if self.prepare_advanced.get():
            self.prepare_advanced_frame.grid(row=3, column=0, sticky="ew", pady=(2, 0))
        else:
            self.prepare_advanced_frame.grid_remove()

    def _toggle_fuzzy_settings(self) -> None:
        enabled = self.override_fuzzy.get()

        def walk(widget) -> None:
            for child in widget.winfo_children():
                if isinstance(child, SelectableLabel):
                    child.set_enabled(enabled)
                else:
                    try:
                        child.configure(state="normal" if enabled else "disabled")
                    except tk.TclError:
                        pass
                walk(child)

        walk(self.fuzzy_frame)

    def _toggle_gcode_advanced(self) -> None:
        if self.gcode_advanced.get():
            self.gcode_advanced_frame.grid(row=4, column=0, sticky="ew", pady=(2, 0))
        else:
            self.gcode_advanced_frame.grid_remove()

    # --- Settings and presets -------------------------------------------------
    # Remembered/saved here: workflow preferences shared across jobs (fuzzy
    # tuning, outline tuning, the disable-wipe/retraction/Z-hop choices). File
    # paths for the current job are deliberately excluded -- those aren't
    # "settings" in the sense a preset or a saved default should capture.

    def _settings_fields(self) -> dict[str, tk.Variable]:
        return {
            "project_slots": self.project_slots,
            "protect_planar": self.protect_planar,
            "protect_overlap": self.protect_overlap,
            "protect_nonmanifold": self.protect_nonmanifold,
            "protect_coincident": self.protect_coincident,
            "disable_wipe": self.disable_wipe,
            "disable_retractions": self.disable_retractions,
            "force_sloped_z_hop": self.force_sloped_z_hop,
            "strip_prepare_attachments": self.strip_prepare_attachments,
            "prepare_advanced": self.prepare_advanced,
            "planar_angle": self.planar_angle,
            "planar_area": self.planar_area,
            "override_fuzzy": self.override_fuzzy,
            "fuzzy_thickness": self.fuzzy_thickness,
            "fuzzy_distance": self.fuzzy_distance,
            "fuzzy_first_layer": self.fuzzy_first_layer,
            "fuzzy_mode": self.fuzzy_mode,
            "fuzzy_noise": self.fuzzy_noise,
            "fuzzy_octaves": self.fuzzy_octaves,
            "fuzzy_persistence": self.fuzzy_persistence,
            "fuzzy_scale": self.fuzzy_scale,
            "gcode_slots": self.gcode_slots,
            "gcode_preset": self.gcode_preset,
            "gcode_advanced": self.gcode_advanced,
            "detour_side": self.detour_side,
            "strip_attachments": self.strip_attachments,
            "detour_depth": self.detour_depth,
            "boundary_resolution": self.boundary_resolution,
            "transition_distance": self.transition_distance,
            "endpoint_coverage": self.endpoint_coverage,
            "minimum_span": self.minimum_span,
            "inner_search": self.inner_search,
            "same_layer_gap": self.same_layer_gap,
        }

    def _collect_settings(self) -> dict[str, object]:
        settings = {name: var.get() for name, var in self._settings_fields().items()}
        settings["_settings_schema"] = SETTINGS_SCHEMA_VERSION
        return settings

    def _apply_settings(self, data: dict[str, object]) -> None:
        fields = self._settings_fields()
        for name, value in data.items():
            variable = fields.get(name)
            if variable is None:
                continue
            try:
                variable.set(value)
            except tk.TclError:
                pass
        self._toggle_prepare_advanced()
        self._toggle_gcode_advanced()
        self._toggle_fuzzy_settings()

    def _load_settings_file(self) -> None:
        data = _load_json_dict(SETTINGS_FILE)
        if data:
            # 0.11 replaces the early debris-detector defaults with the
            # validated, geometry-based smooth-surface profile. Preserve the
            # user's other preferences while allowing these safer defaults to
            # take effect for settings files written by older releases.
            if data.get("_settings_schema") != SETTINGS_SCHEMA_VERSION:
                for key in ("protect_overlap", "protect_nonmanifold", "planar_area"):
                    data.pop(key, None)
            self._apply_settings(data)

    def save_settings(self) -> None:
        _save_json_dict(SETTINGS_FILE, self._collect_settings())
        show_message(
            self, "Settings saved", "FuzzyPaint will open with these settings next time it starts."
        )

    def reset_to_defaults(self) -> None:
        self._apply_settings(self._default_settings)
        self.preset_choice.set("")
        show_message(
            self, "Defaults restored", "All settings have been reset to FuzzyPaint's built-in defaults."
        )

    def _refresh_preset_list(self) -> None:
        self.preset_combo["values"] = sorted(_load_json_dict(PRESETS_FILE))

    def save_preset(self) -> None:
        name = simpledialog.askstring("Save preset", "Name this preset:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return
        presets = _load_json_dict(PRESETS_FILE)
        presets[name] = self._collect_settings()
        _save_json_dict(PRESETS_FILE, presets)
        self._refresh_preset_list()
        self.preset_choice.set(name)

    def apply_selected_preset(self, _event=None) -> None:
        name = self.preset_choice.get()
        data = _load_json_dict(PRESETS_FILE).get(name)
        if data:
            self._apply_settings(data)

    def delete_selected_preset(self) -> None:
        name = self.preset_choice.get()
        if not name:
            return
        presets = _load_json_dict(PRESETS_FILE)
        if name in presets and messagebox.askyesno("Delete preset", f'Delete the preset "{name}"?'):
            del presets[name]
            _save_json_dict(PRESETS_FILE, presets)
            self.preset_choice.set("")
            self._refresh_preset_list()

    def open_github(self) -> None:
        webbrowser.open(GITHUB_URL)

    def show_help(self) -> None:
        existing = getattr(self, "_help_window", None)
        if existing is not None and existing.winfo_exists():
            existing.deiconify()
            existing.lift()
            existing.focus_force()
            return
        window = tk.Toplevel(self)
        self._help_window = window
        window.title(f"About FuzzyPaint {APP_VERSION}")
        window.geometry("720x560")
        window.minsize(600, 460)
        window.transient(self)
        body = ttk.Frame(window, padding=18)
        body.pack(fill="both", expand=True)
        SelectableLabel(body, text="FuzzyPaint", font=("Segoe UI", 18, "bold"), width=12).pack(anchor="w")
        SelectableLabel(
            body, text="for Bambu Studio", font=("Segoe UI", 12), foreground="#555555", width=20
        ).pack(anchor="w")
        SelectableLabel(
            body, text=f"Version {APP_VERSION}", font=("Segoe UI", 12), foreground="#555555", width=20
        ).pack(anchor="w", pady=(1, 12))
        help_text = (
            "# Why FuzzyPaint exists\n\n"
            "FuzzyPaint was created to add fuzzy skin only to the colour-painted areas of a model, "
            "allowing those details to stand out even when the model is printed with a single colour.\n\n"
            "If fuzzy skin is not wanted, FuzzyPaint can instead create a physical outline around the "
            "painted areas. These textured or outlined regions remain easy to identify when painting the "
            "finished model by hand.\n\n"
            "On complex models with detailed paintwork, replacing hundreds of filament changes with a "
            "single-filament print can save hundreds of hours of printing time and a substantial amount "
            "of purge waste.\n\n"
            "# How the two steps work\n\n"
            "## 1. Prepare project\n\n"
            "Choose a colour-painted Bambu Studio 3MF project. FuzzyPaint keeps the colour painting "
            "needed by step 2, adds fuzzy texture to the selected painted colour, and creates a separate "
            "project ready for multicolour slicing.\n\n"
            "## 2. Create a single-filament file\n\n"
            "Choose the prepared project and FuzzyPaint will run Bambu Studio's slicer automatically. "
            "Alternatively, choose a plate sliced file exported manually. FuzzyPaint uses the colour "
            "boundaries to add physical outline detail, then removes filament changes and "
            "the purge tower. The finished file prints from one spool. You can use this step without fuzzy skin.\n\n"
            "## Wipe while retracting\n\n"
            "The prepared project disables ordinary wipe-while-retracting moves by default. Bambu Studio "
            "then plans its retractions with wiping disabled before the sliced file is created. Untick the "
            "option on either step if a material or model prints better with wiping enabled.\n\n"
            "## Retractions\n\n"
            "Ordinary model retractions are disabled for every virtual filament by default. This is separate "
            "from wipe while retracting. Untick the option on either step if the model or material needs "
            "retraction to control stringing. Tool-change retractions are removed with their complete "
            "colour-change blocks.\n\n"
            "## Sloped Z-hop\n\n"
            "Sloped Z-hop is selected by default for the process and every filament. Existing Z-hop heights "
            "are retained; the option changes only the lift type.\n\n"
            "## Which colour number?\n\n"
            "Use the filament slot number shown beside the painted colour in Bambu Studio. Multiple slots "
            "can be entered as 2,4.\n\n"
            "## Settings, presets and the toolbar\n\n"
            "Save Settings remembers your current choices as the ones FuzzyPaint opens with next time. "
            "Reset to Defaults restores FuzzyPaint's original built-in values without touching any saved "
            "preset. Presets are named snapshots of the same settings, switchable from the toolbar, useful "
            "when different models need different fuzzy or outline tuning.\n\n"
            "## Technical detail\n\n"
            "Bambu stores colour and fuzzy painting as per-triangle masks inside the 3MF. The first step "
            "copies selected colour-mask regions into the fuzzy-skin mask. The second step analyses outer-wall "
            "toolpaths in exported G-code, inserts inward detours at colour boundaries, and collapses the job "
            "to one physical filament. Other project and print-profile data is retained."
        )
        text_frame = ttk.Frame(body)
        text_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(text_frame, orient="vertical")
        text = tk.Text(
            text_frame,
            wrap="word",
            relief="flat",
            background=window.cget("background"),
            padx=2,
            pady=10,
            yscrollcommand=scrollbar.set,
        )
        scrollbar.configure(command=text.yview)
        _insert_formatted(text, help_text)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        ttk.Button(body, text="Close", command=window.destroy).pack(anchor="e", pady=(8, 0))

    def _apply_preset(self) -> None:
        values = PRESETS.get(self.gcode_preset.get())
        if not values:
            return
        variables = (
            self.detour_depth, self.boundary_resolution, self.transition_distance,
            self.endpoint_coverage, self.minimum_span, self.inner_search, self.same_layer_gap,
        )
        for variable, value in zip(variables, values):
            variable.set(f"{value:.2f}")

    def choose_project_source(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose a colour-painted project", filetypes=[("3MF projects", "*.3mf"), ("All files", "*.*")]
        )
        if filename:
            self.project_source.set(filename)
            path = Path(filename)
            self.project_destination.set(
                str(suggested_output_path(path, suggest_prepared_project_name(path.name)))
            )

    def choose_project_destination(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Save prepared project", defaultextension=".3mf", filetypes=[("3MF projects", "*.3mf")],
            initialfile=Path(self.project_destination.get()).name if self.project_destination.get() else "model (FP-Prepped).3mf",
        )
        if filename:
            self.project_destination.set(filename)

    def choose_gcode_source(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose a prepared project or Bambu plate sliced file",
            filetypes=[("3MF projects and sliced packages", "*.3mf"), ("All files", "*.*")],
        )
        if filename:
            self.gcode_source.set(filename)
            path = Path(filename)
            self.gcode_destination.set(
                str(suggested_output_path(path, suggest_print_package_name(path.name)))
            )

    def choose_gcode_destination(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Save processed sliced file", defaultextension=".gcode.3mf",
            filetypes=[("Bambu sliced packages", "*.gcode.3mf"), ("All files", "*.*")],
            initialfile=Path(self.gcode_destination.get()).name if self.gcode_destination.get() else "model (FP-Print_Ready).gcode.3mf",
        )
        if filename:
            self.gcode_destination.set(filename)

    @staticmethod
    def _float(variable: tk.StringVar, label: str, *, positive=False, zero_ok=True) -> float:
        try:
            value = float(variable.get())
        except ValueError as exc:
            raise ConversionError(f"{label} must be a number") from exc
        if positive and (value < 0 if zero_ok else value <= 0):
            raise ConversionError(f"{label} must be {'zero or greater' if zero_ok else 'greater than zero'}")
        return value

    def _fuzzy_settings(self) -> dict[str, object] | None:
        if not self.override_fuzzy.get():
            return None
        settings = {
            "fuzzy_skin_thickness": self._float(self.fuzzy_thickness, "Fuzzy thickness", positive=True, zero_ok=False),
            "fuzzy_skin_point_distance": self._float(self.fuzzy_distance, "Fuzzy point distance", positive=True, zero_ok=False),
            "fuzzy_skin_first_layer": int(self.fuzzy_first_layer.get()),
            "fuzzy_skin_mode": self.fuzzy_mode.get().strip(),
            "fuzzy_skin_noise_type": self.fuzzy_noise.get().strip(),
            "fuzzy_skin_octaves": int(self._float(self.fuzzy_octaves, "Noise octaves", positive=True, zero_ok=False)),
            "fuzzy_skin_persistence": self._float(self.fuzzy_persistence, "Noise persistence", positive=True),
            "fuzzy_skin_scale": self._float(self.fuzzy_scale, "Noise scale", positive=True, zero_ok=False),
        }
        if not settings["fuzzy_skin_mode"] or not settings["fuzzy_skin_noise_type"]:
            raise ConversionError("Fuzzy mode and noise type cannot be empty")
        return settings

    def prepare_project(self) -> None:
        if not self.project_source.get().strip() or not self.project_destination.get().strip():
            show_message(self, "Missing file", "Choose both an input and output project.")
            return
        try:
            selected_slots = parse_slots(self.project_slots.get())
            angle = self._float(self.planar_angle, "Flat-surface angle", positive=True)
            area = self._float(self.planar_area, "Minimum flat area", positive=True, zero_ok=False)
            fuzzy_settings = self._fuzzy_settings()
        except (ConversionError, ValueError) as exc:
            show_message(self, "Invalid setting", str(exc))
            return
        destination = self._avoid_overwrite(Path(self.project_destination.get()), self.project_destination)
        source = self.project_source.get()
        protect_planar = self.protect_planar.get()
        protect_overlap = self.protect_overlap.get()
        protect_nonmanifold = self.protect_nonmanifold.get()
        protect_coincident = self.protect_coincident.get()
        disable_wipe = self.disable_wipe.get()
        disable_retractions = self.disable_retractions.get()
        force_sloped_z_hop = self.force_sloped_z_hop.get()
        strip_prepare_attachments = self.strip_prepare_attachments.get()

        def work():
            started = time.perf_counter()
            report = convert_3mf(
                source, destination, slots=selected_slots, remove_colour=False,
                protect_planar=protect_planar, planar_angle_degrees=angle,
                planar_minimum_area_mm2=area,
                planar_compact_strip_minimum_area_mm2=30.0 if protect_planar else None,
                protect_connected_coplanar=protect_planar,
                connected_coplanar_angle_degrees=angle,
                connected_coplanar_plane_tolerance_mm=0.25,
                connected_coplanar_minimum_short_span_mm=2.5,
                connected_coplanar_minimum_long_span_mm=6.0,
                connected_coplanar_satellite_gap_mm=0.5,
                protect_flat_contact=protect_planar,
                protect_fuzz_envelope=protect_planar,
                fuzz_envelope_distance_mm=(
                    float(fuzzy_settings["fuzzy_skin_thickness"])
                    if fuzzy_settings is not None else 0.25
                ),
                fuzz_envelope_contact_neighbor_rings=3,
                protect_overlap=protect_overlap,
                protect_nonmanifold=protect_nonmanifold,
                protect_coincident=protect_coincident,
                single_colour=False, fuzzy_settings=fuzzy_settings,
                disable_wipe_while_retracting=disable_wipe,
                disable_retractions=disable_retractions,
                force_sloped_z_hop=force_sloped_z_hop,
                strip_nonprint_auxiliaries=strip_prepare_attachments,
            )
            return report, time.perf_counter() - started

        def done(result):
            report, elapsed_seconds = result
            if elapsed_seconds >= 60:
                minutes, seconds = divmod(elapsed_seconds, 60)
                elapsed_text = f"{int(minutes)} min {seconds:.1f} sec"
            else:
                elapsed_text = f"{elapsed_seconds:.1f} sec"
            self.prepare_status.set(f"Prepared for slicing:\n{destination}")
            self.gcode_source.set(str(destination))
            self.gcode_destination.set(str(destination.with_name(suggest_print_package_name(destination.name))))
            self.gcode_status.set(
                "Prepared project selected. Create the single-filament print file when ready."
            )
            self.notebook.select(self.gcode_tab)
            fuzzy_summary = (
                f"Fuzzy thickness: {fuzzy_settings['fuzzy_skin_thickness']} mm\n"
                f"Fuzzy point distance: {fuzzy_settings['fuzzy_skin_point_distance']} mm\n\n"
                if fuzzy_settings else
                "Fuzzy process settings: preserved from the source project\n\n"
            )
            show_message(
                self,
                "Project ready",
                f"Created {destination.name}\n\nPreparation time: {elapsed_text}\n\n"
                f"{format_report(report)}\n\n{fuzzy_summary}"
                "Next, use the second tab to create the print file. FuzzyPaint can now run Bambu Studio's slicer automatically.",
            )

        self._start_job(self.prepare_button, self.prepare_status, "Preparing project…", work, done)

    @staticmethod
    def _parse_overrides(text: str) -> list[tuple[int, int, int]]:
        result = []
        for value in (item.strip() for item in text.split(",") if item.strip()):
            match = re.fullmatch(r"(\d+)-(\d+):(\d+)", value)
            if not match:
                raise ConversionError("Paint correction ranges must use FIRST-LAST:FILAMENT")
            first, last, filament = map(int, match.groups())
            if first < 1 or last < first or filament < 1:
                raise ConversionError("Paint correction ranges must contain positive, ordered numbers")
            result.append((first, last, filament - 1))
        return result

    def process_gcode(self) -> None:
        if not self.gcode_source.get().strip() or not self.gcode_destination.get().strip():
            show_message(self, "Missing file", "Choose both an input and output sliced file.")
            return
        try:
            slots = parse_slots(self.gcode_slots.get())
            if slots is None:
                raise ConversionError("Choose the painted filament slot or slots; 'all' is not valid here")
            depth = self._float(self.detour_depth, "Detour depth", positive=True, zero_ok=False)
            resolution = self._float(self.boundary_resolution, "Detection resolution", positive=True, zero_ok=False)
            transition = self._float(self.transition_distance, "Transition distance", positive=True)
            coverage = self._float(self.endpoint_coverage, "Endpoint coverage", positive=True)
            minimum = self._float(self.minimum_span, "Minimum detected span", positive=True)
            inner_search = self._float(self.inner_search, "Inner-wall search", positive=True, zero_ok=False)
            gap = self._float(self.same_layer_gap, "Same-layer gap", positive=True)
            overrides = self._parse_overrides(self.filament_overrides.get())
        except ConversionError as exc:
            show_message(self, "Invalid setting", str(exc))
            return
        side = {"Both — complete outline": "both", "Above painted regions": "upper", "Below painted regions": "lower"}[
            self.detour_side.get()
        ]
        source = Path(self.gcode_source.get())
        destination = self._avoid_overwrite(Path(self.gcode_destination.get()), self.gcode_destination)
        strip_attachments = self.strip_attachments.get()
        disable_retractions = self.disable_retractions.get()
        disable_wipe_while_retracting = self.disable_wipe.get()
        force_sloped_z_hop = self.force_sloped_z_hop.get()

        def work():
            started = time.perf_counter()
            phase_timings: dict[str, float] = {}
            automatically_sliced = not archive_has_gcode(source)
            phase_started = time.perf_counter()
            expected_fuzzy = self._project_fuzzy_settings(source) if automatically_sliced else None
            phase_timings["Inspect project"] = time.perf_counter() - phase_started
            with tempfile.TemporaryDirectory(prefix="c2f-slice-", dir=destination.parent) as temporary:
                sliced_source = source
                if automatically_sliced:
                    phase_started = time.perf_counter()
                    configured_source = Path(temporary) / "automatic-slice-input.3mf"
                    configure_project_for_slicing(
                        source,
                        configured_source,
                        disable_retractions=disable_retractions,
                        disable_wipe_while_retracting=disable_wipe_while_retracting,
                        force_sloped_z_hop=force_sloped_z_hop,
                    )
                    phase_timings["Configure slice input"] = time.perf_counter() - phase_started
                    phase_started = time.perf_counter()
                    sliced_source = Path(temporary) / "automatic-slice.3mf"
                    slice_project(configured_source, sliced_source)
                    phase_timings["Bambu Studio slicing"] = time.perf_counter() - phase_started
                if fuzzypaint_engine is not None:
                    # Rust engine (see fuzzypaint_engine/): parses, plans,
                    # emits and collapses in one call. Falls back to the
                    # pure-Python path below if the extension isn't built.
                    phase_started = time.perf_counter()
                    engine_result = fuzzypaint_engine.process_gcode_3mf(
                        str(sliced_source), str(destination),
                        inset_mm=depth, boundary_resolution_mm=resolution,
                        transition_distance_mm=transition, endpoint_coverage_mm=coverage,
                        inner_search_mm=inner_search, minimum_span_mm=minimum, same_layer_gap_mm=gap,
                        detour_side=side, subject_filaments={slot - 1 for slot in slots},
                        filament_overrides=overrides, strip_nonprint_auxiliaries=strip_attachments,
                    )
                    detour_count = engine_result.pop("detour_count")
                    report = CollapseReport(**engine_result)
                    phase_timings["Rust G-code processing"] = time.perf_counter() - phase_started
                else:
                    phase_started = time.perf_counter()
                    replacements = build_interval_detours(
                        sliced_source, inset_mm=depth, boundary_resolution_mm=resolution,
                        transition_distance_mm=transition, endpoint_coverage_mm=coverage,
                        inner_search_mm=inner_search, minimum_span_mm=minimum, same_layer_gap_mm=gap,
                        detour_side=side, subject_filaments={slot - 1 for slot in slots},
                        filament_overrides=overrides,
                    )
                    detour_count = len(replacements)
                    phase_timings["Python detour planning"] = time.perf_counter() - phase_started
                    phase_started = time.perf_counter()
                    report = collapse_gcode_3mf(
                        sliced_source,
                        destination,
                        line_replacements=replacements,
                        strip_nonprint_auxiliaries=strip_attachments,
                    )
                    phase_timings["Python package rewriting"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
            validation = self._validate_sliced_package(destination)
            phase_timings["Validate output"] = time.perf_counter() - phase_started
            if expected_fuzzy is not None:
                actual_fuzzy = (validation["fuzzy_thickness"], validation["fuzzy_distance"])
                if any(abs(float(actual) - float(expected)) > 1e-9 for actual, expected in zip(actual_fuzzy, expected_fuzzy)):
                    raise ConversionError(
                        "Bambu Studio sliced with different fuzzy settings than the prepared project: "
                        f"expected {expected_fuzzy[0]} / {expected_fuzzy[1]}, "
                        f"but G-code contains {actual_fuzzy[0]} / {actual_fuzzy[1]}"
                    )
            elapsed_seconds = time.perf_counter() - started
            return detour_count, report, validation, elapsed_seconds, automatically_sliced, phase_timings

        def done(result):
            detour_count, report, validation, elapsed_seconds, automatically_sliced, phase_timings = result

            def elapsed_text(seconds: float) -> str:
                if seconds >= 60:
                    minutes, remainder = divmod(seconds, 60)
                    return f"{int(minutes)} min {remainder:.1f} sec"
                return f"{seconds:.1f} sec"

            timing_summary = "\n".join(
                f"  {name}: {elapsed_text(seconds)}" for name, seconds in phase_timings.items()
            )
            summary = (
                f"Created {destination.name}\n\nTotal processing time: {elapsed_text(elapsed_seconds)}\n"
                f"Phase timings:\n{timing_summary}\n"
                f"Automatic Bambu Studio slice: {'yes' if automatically_sliced else 'not needed'}\n"
                f"Retractions disabled before slicing: "
                f"{'yes' if automatically_sliced and disable_retractions else 'no'}\n"
                f"Wipe while retracting disabled before slicing: "
                f"{'yes' if automatically_sliced and disable_wipe_while_retracting else 'no'}\n"
                f"Sloped Z-hop forced before slicing: "
                f"{'yes' if automatically_sliced and force_sloped_z_hop else 'no'}\n"
                f"Detoured source moves: {detour_count:,}\n"
                f"Colour changes removed: {report.toolchanges_removed:,}\n"
                f"Purge-tower blocks removed: {report.tower_blocks_removed:,}\n"
                f"Obsolete purge-tower trips removed: {report.tower_excursions_removed:,}\n"
                f"Orphaned unretractions removed: {report.orphan_unretractions_removed:,}\n"
                f"Estimated model filament: {report.filament_weight_g:.2f} g\n"
                f"Recalculated model time: {report.estimated_model_seconds / 60:.1f} min\n"
                f"Non-print attachments removed: {report.attachments_removed:,} "
                f"({report.attachment_bytes_removed / 1_000_000:.1f} MB)\n"
                f"Package CRC: {validation['crc']}\nEmbedded G-code checksum: {validation['md5']}\n"
                f"Sliced fuzzy thickness: {validation['fuzzy_thickness']} mm\n"
                f"Sliced fuzzy point distance: {validation['fuzzy_distance']} mm\n"
                f"Remaining colour-change blocks: {validation['toolchanges']}"
            )
            self.gcode_status.set(f"Ready for Bambu Studio Preview:\n{destination}")
            reveal_in_explorer(destination)
            show_message(self, "Sliced-file processing complete", summary)

        self._start_job(
            self.gcode_button,
            self.gcode_status,
            "Slicing if needed, then analysing and rebuilding toolpaths…",
            work,
            done,
        )

    @staticmethod
    def _project_fuzzy_settings(path: Path) -> tuple[str, str] | None:
        """Read thickness/distance requested by an editable project, if present."""
        try:
            with zipfile.ZipFile(path) as archive:
                settings = json.loads(archive.read("Metadata/project_settings.config"))
        except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            return None
        thickness = settings.get("fuzzy_skin_thickness")
        distance = settings.get("fuzzy_skin_point_distance")
        if thickness is None or distance is None:
            return None
        return str(thickness), str(distance)

    @staticmethod
    def _validate_sliced_package(path: Path) -> dict[str, object]:
        with zipfile.ZipFile(path) as archive:
            bad = archive.testzip()
            gcode_name = next((name for name in archive.namelist() if name.lower().endswith(".gcode")), None)
            if not gcode_name:
                raise ConversionError("Processed package contains no G-code")
            data = archive.read(gcode_name)
            md5_name = next((name for name in archive.namelist() if name.lower().endswith(".gcode.md5")), None)
            expected = archive.read(md5_name).decode("ascii", "ignore").strip().split()[0] if md5_name else ""
            actual = hashlib.md5(data).hexdigest()
            if expected and expected != actual:
                raise ConversionError("Processed package G-code checksum does not match")
            startup = data.split(b"; layer num/total_layer_count:", 1)[0]
            def setting(name: bytes) -> str:
                match = re.search(rb"(?m)^; " + re.escape(name) + rb" = ([^\r\n]+)", data)
                return match.group(1).decode("utf-8", "replace").strip() if match else "not recorded"
            for line in startup.splitlines():
                stripped = line.strip()
                ams_match = re.match(rb"^M62[01]\s+S(\d+)A(?:\s|$)", stripped, re.IGNORECASE)
                tool_match = re.match(rb"^T(\d+)(?:\s*(?:;.*)?)$", stripped, re.IGNORECASE)
                selection = (
                    int(ams_match.group(1)) if ams_match else
                    int(tool_match.group(1)) if tool_match else None
                )
                if selection is not None and 0 < selection < 255:
                    raise ConversionError(
                        f"Processed package still selects obsolete startup filament {selection + 1}"
                    )
            return {
                "crc": "clean" if bad is None else f"FAILED at {bad}",
                "md5": "valid" if not expected or expected == actual else "invalid",
                "toolchanges": data.count(b"; CP TOOLCHANGE START"),
                "fuzzy_thickness": setting(b"fuzzy_skin_thickness"),
                "fuzzy_distance": setting(b"fuzzy_skin_point_distance"),
            }

    def _start_job(self, button, status_variable, status_text, worker, done) -> None:
        if self.job_queue is not None:
            show_message(self, "Busy", "Another conversion is already running.")
            return
        self.job_queue = queue.Queue()
        self.job_button = button
        self.job_done = done
        button.state(["disabled"])
        status_variable.set(status_text)
        self.progress.start(12)

        def run():
            try:
                self.job_queue.put(("ok", worker()))
            except Exception as exc:
                self.job_queue.put(("error", exc))

        threading.Thread(target=run, daemon=True).start()
        self.after(100, lambda: self._poll_job(status_variable))

    def _poll_job(self, status_variable) -> None:
        assert self.job_queue is not None
        try:
            result_type, payload = self.job_queue.get_nowait()
        except queue.Empty:
            self.after(100, lambda: self._poll_job(status_variable))
            return
        button, done = self.job_button, self.job_done
        self.job_queue = self.job_button = self.job_done = None
        self.progress.stop()
        if button is not None:
            button.state(["!disabled"])
        if result_type == "error":
            status_variable.set(f"Failed: {payload}")
            show_message(self, "Conversion failed", str(payload))
        else:
            done(payload)


def _declare_dpi_aware() -> None:
    """Tell Windows this process handles its own DPI scaling.

    Without this, an unmanifested Tk app is treated as DPI-unaware: Windows
    silently bitmap-scales its window and can reposition/resize it oddly on
    a multi-monitor setup where monitors use different scaling factors --
    exactly the kind of "window jumped to the other monitor" behaviour this
    can cause. Must run before the first Tk window is created. Best-effort:
    harmless if it fails (non-Windows, or an old Windows without shcore).
    """
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # system DPI aware
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        # Used by release packaging to verify that imports, the bundled Tcl/Tk
        # runtime, and the Rust extension are all usable. Keep the short-lived
        # Tk root withdrawn so the self-test does not flash a window.
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        if "--require-rust" in sys.argv and fuzzypaint_engine is None:
            raise SystemExit(2)
        raise SystemExit(0)
    _declare_dpi_aware()
    App().mainloop()
