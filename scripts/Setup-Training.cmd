@echo off
setlocal
if not exist "%~dp0local-env.cmd" (
    echo Missing scripts\local-env.cmd. See docs\environment.md.
    exit /b 2
)
call "%~dp0local-env.cmd"
if errorlevel 1 exit /b %errorlevel%
set "UV_PROJECT_ENVIRONMENT=%~dp0..\training\.venv"
set "UV_PYTHON_DOWNLOADS=never"
set "UV_HTTP_TIMEOUT=120"
"%FORGEPILOT_UV%" sync --locked --project "%~dp0..\training" --python "%FORGEPILOT_PYTHON%" --system-certs --link-mode copy
if errorlevel 1 exit /b %errorlevel%
"%FORGEPILOT_UV%" pip check --python "%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe"
exit /b %errorlevel%
