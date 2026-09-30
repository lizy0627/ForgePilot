@echo off
setlocal
set "TRAINING_PYTHON=%~dp0..\training\.venv\Scripts\python.exe"
if not exist "%TRAINING_PYTHON%" (
    echo Training environment missing. Run scripts\Setup-Training.cmd first.
    exit /b 2
)
set "PYTHONIOENCODING=utf-8"
"%TRAINING_PYTHON%" "%~dp0..\training\check_environment.py" %*
exit /b %errorlevel%
