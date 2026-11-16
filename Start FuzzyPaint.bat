@echo off
setlocal
cd /d "%~dp0"
set "FUZZYPAINT_PYTHONW=%LOCALAPPDATA%\Programs\Python\Python315\pythonw.exe"
if exist "%FUZZYPAINT_PYTHONW%" (
    start "FuzzyPaint" "%FUZZYPAINT_PYTHONW%" "%~dp0fuzzypaint_gui.pyw"
) else (
    echo FuzzyPaint 0.12.x requires Python 3.15.2.
    echo Install it with: winget install --id Python.Python.3.15 --exact --scope user
    pause
)
