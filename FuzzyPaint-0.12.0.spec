# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
import zipfile


project_root = Path(SPECPATH)
engine_candidates = list(
    (project_root / ".venv-build-315" / "Lib" / "site-packages" / "fuzzypaint_engine").glob(
        "fuzzypaint_engine*.pyd"
    )
)
if len(engine_candidates) != 1:
    raise RuntimeError(
        "Expected exactly one Python 3.15 fuzzypaint_engine extension; "
        f"found {engine_candidates!r}"
    )
engine_extension = engine_candidates[0]

# Python 3.15's Windows installer ships the Tcl 9 and Tk 9 script libraries as
# zip archives. PyInstaller 6.22 does not discover those archives yet, so expose
# extracted copies while Analysis runs. Its normal tkinter hook then collects
# them under _tcl_data/_tk_data and installs the standard runtime hook.
python_root = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "Python" / "Python315"
tk_runtime = project_root / "build" / "python315-tk-runtime"
tcl_library = tk_runtime / "tcl_library"
tk_library = tk_runtime / "tk_library"
if not (tcl_library / "init.tcl").is_file():
    tk_runtime.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(python_root / "tcl" / "libtcl9.0.4.zip") as archive:
        archive.extractall(tk_runtime)
if not (tk_library / "tk.tcl").is_file():
    tk_runtime.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(python_root / "tcl" / "libtk9.0.4.zip") as archive:
        archive.extractall(tk_runtime)
os.environ["TCL_LIBRARY"] = str(tcl_library)
os.environ["TK_LIBRARY"] = str(tk_library)

a = Analysis(
    ["fuzzypaint_gui.pyw"],
    # Put the compiled extension ahead of the same-named Rust crate directory.
    # Otherwise that directory can be frozen as an empty namespace package and
    # shadow the bundled .pyd at runtime.
    pathex=[str(engine_extension.parent)],
    binaries=[(str(engine_extension), ".")],
    datas=[
        (str(project_root / "LICENSE"), "."),
        (str(project_root / "ASSET_NOTICE.md"), "."),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="FuzzyPaint-0.12.0",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="FuzzyPaint-0.12.0",
)
