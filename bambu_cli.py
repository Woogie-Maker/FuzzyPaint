"""Small, testable wrapper around Bambu Studio's command-line slicer."""

from __future__ import annotations

import os
import shutil
import subprocess
import zipfile
from pathlib import Path


class BambuSlicingError(RuntimeError):
    """Raised when Bambu Studio cannot create a sliced 3MF package."""


def archive_has_gcode(path: Path) -> bool:
    """Return whether a 3MF archive already contains printable G-code."""
    try:
        with zipfile.ZipFile(path) as archive:
            return any(name.lower().endswith(".gcode") for name in archive.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def find_bambu_studio() -> Path:
    """Locate Bambu Studio, preferring an explicit environment override."""
    candidates: list[Path] = []
    override = os.environ.get("BAMBU_STUDIO_EXE")
    if override:
        candidates.append(Path(override))

    discovered = shutil.which("bambu-studio") or shutil.which("bambu-studio.exe")
    if discovered:
        candidates.append(Path(discovered))

    candidates.extend(
        [
            Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
            / "Bambu Studio"
            / "bambu-studio.exe",
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Programs"
            / "Bambu Studio"
            / "bambu-studio.exe",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise BambuSlicingError(
        "Bambu Studio was not found. Install it, set BAMBU_STUDIO_EXE, or slice and export the plate manually."
    )


def build_slice_command(executable: Path, source: Path, destination: Path) -> list[str]:
    """Build the minimal command documented by Bambu Studio."""
    return [
        str(executable),
        "--slice",
        "0",
        "--debug",
        "2",
        "--export-3mf",
        str(destination),
        str(source),
    ]


def slice_project(source: Path, destination: Path, *, executable: Path | None = None) -> Path:
    """Slice plate 1 of a prepared project into a 3MF containing G-code."""
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise BambuSlicingError(f"Prepared project does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    executable = executable or find_bambu_studio()

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        build_slice_command(executable, source, destination),
        cwd=source.parent,
        capture_output=True,
        text=True,
        creationflags=creation_flags,
        check=False,
    )
    if completed.returncode != 0 or not destination.is_file() or not archive_has_gcode(destination):
        detail = (completed.stderr or completed.stdout or "").strip()
        if detail:
            detail = f"\n\nBambu Studio reported:\n{detail[-1200:]}"
        raise BambuSlicingError(
            "Bambu Studio could not slice the prepared project automatically. "
            "Try again, or slice and export the plate manually."
            + detail
        )
    return destination
