@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0training\.venv\Scripts\python.exe" (
    echo Training environment missing. Run scripts\Setup-Training.cmd first.
    pause
    exit /b 2
)
"%~dp0training\.venv\Scripts\python.exe" "%~dp0training\studio.py"
if errorlevel 1 pause
