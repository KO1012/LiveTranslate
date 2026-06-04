@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\smoke_check.py
) else (
    python scripts\smoke_check.py
)
