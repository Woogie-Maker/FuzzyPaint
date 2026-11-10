@echo off
setlocal
cd /d "%~dp0"
start "FuzzyPaint" pyw -3 "%~dp0fuzzypaint_gui.pyw"
