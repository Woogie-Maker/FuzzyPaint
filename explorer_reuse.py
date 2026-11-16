"""Reuse an exact matching Windows Explorer folder window when possible."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


_ENUMERATE_EXPLORER_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
$windows = @(
    (New-Object -ComObject Shell.Application).Windows() | ForEach-Object {
        try {
            if ([IO.Path]::GetFileName($_.FullName) -ieq 'explorer.exe') {
                $folder = $_.Document.Folder.Self.Path
                if ($folder) {
                    [pscustomobject]@{
                        hwnd = [int64]$_.HWND
                        path = [string]$folder
                    }
                }
            }
        } catch {
            # ShellWindows also contains transient/non-filesystem windows.
        }
    })
)
ConvertTo-Json -Compress -InputObject $windows
"""


@dataclass(frozen=True)
class ExplorerWindow:
    hwnd: int
    path: Path


def canonical_folder_path(path: os.PathLike[str] | str) -> str:
    """Return a normalized, real, case-insensitive filesystem path key."""
    return os.path.normcase(
        os.path.realpath(os.path.abspath(os.path.normpath(os.fspath(path))))
    ).casefold()


def enumerate_explorer_windows() -> list[ExplorerWindow]:
    """Read filesystem-backed Explorer folder windows through Shell.Application."""
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                _ENUMERATE_EXPLORER_WINDOWS,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=8,
            creationflags=creationflags,
        )
        payload = json.loads(result.stdout or "[]")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    windows = []
    for item in payload:
        try:
            hwnd = int(item["hwnd"])
            path = Path(item["path"])
        except (KeyError, TypeError, ValueError):
            continue
        if hwnd > 0 and str(path):
            windows.append(ExplorerWindow(hwnd, path))
    return windows


def find_exact_folder_window(
    folder: os.PathLike[str] | str,
    windows: Iterable[ExplorerWindow],
) -> ExplorerWindow | None:
    """Find only a window whose canonical filesystem path exactly matches."""
    wanted = canonical_folder_path(folder)
    for window in windows:
        if canonical_folder_path(window.path) == wanted:
            return window
    return None


def focus_explorer_window(hwnd: int) -> bool:
    """Restore and request foreground focus for an existing Explorer window."""
    if os.name != "nt" or hwnd <= 0:
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.ShowWindowAsync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.ShowWindowAsync.restype = ctypes.c_bool
        user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        user32.BringWindowToTop.restype = ctypes.c_bool
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        user32.SetForegroundWindow.restype = ctypes.c_bool
        handle = ctypes.c_void_p(hwnd)
        user32.ShowWindowAsync(handle, 9)  # SW_RESTORE
        user32.BringWindowToTop(handle)
        return bool(user32.SetForegroundWindow(handle))
    except (AttributeError, OSError):
        return False


def reveal_in_explorer(path: Path) -> None:
    """Focus the exact output folder if open; otherwise open one new window."""
    folder = path.parent
    existing = find_exact_folder_window(folder, enumerate_explorer_windows())
    if existing is not None:
        focus_explorer_window(existing.hwnd)
        return
    try:
        subprocess.Popen(["explorer.exe", str(folder)])
    except OSError:
        pass
