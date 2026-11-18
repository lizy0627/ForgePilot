@echo off
setlocal

for %%I in (uv.exe) do set "UV_EXE=%%~$PATH:I"
if not defined UV_EXE (
    echo ERROR: uv was not found on PATH.
    exit /b 2
)

for %%I in ("%~dp0..") do set "FORGEPILOT_ROOT=%%~fI"
if exist "%FORGEPILOT_ROOT%\agent\.venv" (
    echo WARNING: agent\.venv is not a supported ForgePilot environment. It was not removed.
)

if exist "%~dp0local-env.cmd" call "%~dp0local-env.cmd"
if defined FORGEPILOT_PYTHON (
    if not exist "%FORGEPILOT_PYTHON%" (
        echo ERROR: Configured FORGEPILOT_PYTHON does not exist.
        exit /b 2
    )
    set "FORGEPILOT_SETUP_PYTHON=%FORGEPILOT_PYTHON%"
) else if exist "%FORGEPILOT_ROOT%\agent.venv\Scripts\python.exe" (
    set "FORGEPILOT_SETUP_PYTHON=%FORGEPILOT_ROOT%\agent.venv\Scripts\python.exe"
) else (
    for /f "delims=" %%I in ('where "$PATH:python.exe" 2^>nul') do call :try_path_python "%%~fI"
    if not defined FORGEPILOT_SETUP_PYTHON (
        echo ERROR: Python 3.12 was not found on PATH.
        exit /b 2
    )
)
"%FORGEPILOT_SETUP_PYTHON%" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>nul
if errorlevel 1 (
    echo ERROR: Selected Python must be Python 3.12.
    exit /b 2
)

set "UV_PROJECT_ENVIRONMENT=%FORGEPILOT_ROOT%\agent.venv"
set "UV_PYTHON=%FORGEPILOT_SETUP_PYTHON%"
set "UV_PYTHON_DOWNLOADS=never"

pushd "%FORGEPILOT_ROOT%\agent"
"%UV_EXE%" sync --locked --python "%FORGEPILOT_SETUP_PYTHON%"
set "FORGEPILOT_SETUP_EXIT=%errorlevel%"
popd

if not "%FORGEPILOT_SETUP_EXIT%"=="0" (
    echo ERROR: Agent environment setup failed. No successful setup was reported.
    exit /b %FORGEPILOT_SETUP_EXIT%
)
if not exist "%UV_PROJECT_ENVIRONMENT%\Scripts\python.exe" (
    echo ERROR: uv completed without creating agent.venv\Scripts\python.exe.
    exit /b 1
)

echo ForgePilot agent environment is ready at "%UV_PROJECT_ENVIRONMENT%".
exit /b 0

:try_path_python
if defined FORGEPILOT_SETUP_PYTHON exit /b 0
"%~1" -c "import sys; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>nul
if not errorlevel 1 set "FORGEPILOT_SETUP_PYTHON=%~1"
exit /b 0
