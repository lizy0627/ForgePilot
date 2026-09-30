@echo off
setlocal
chcp 65001 >nul
set "PYTHONIOENCODING=utf-8"
if not exist "%~dp0training\.venv\Scripts\python.exe" exit /b 0
"%~dp0training\.venv\Scripts\python.exe" "%~dp0training\studio.py" --stop
