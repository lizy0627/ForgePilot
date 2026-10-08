@echo off
set "FORGEPILOT_ENV_MODE=%~1"
if not defined FORGEPILOT_ENV_MODE set "FORGEPILOT_ENV_MODE=all"
set "FORGEPILOT_ENV_ERROR=0"
set "FORGEPILOT_CTEST="

if exist "%~dp0local-env.cmd" (
    call "%~dp0local-env.cmd"
    if errorlevel 1 (
        echo ERROR: scripts\local-env.cmd could not be loaded.
        exit /b 2
    )
) else if /i not "%FORGEPILOT_ENV_MODE%"=="runtime" (
    echo ERROR: scripts\local-env.cmd is missing.
    echo Run: copy scripts\local-env.example.cmd scripts\local-env.cmd
    exit /b 2
)

if /i "%FORGEPILOT_ENV_MODE%"=="runtime" goto check_python
if /i "%FORGEPILOT_ENV_MODE%"=="build" goto check_build
if /i "%FORGEPILOT_ENV_MODE%"=="client-test" goto check_client_test
if /i not "%FORGEPILOT_ENV_MODE%"=="all" (
    echo ERROR: Unknown environment check mode.
    exit /b 2
)

:check_python
set "FORGEPILOT_SELECTED_PYTHON="
set "FORGEPILOT_CONFIGURED_PYTHON=%FORGEPILOT_PYTHON%"
if defined FORGEPILOT_CONFIGURED_PYTHON if exist "%FORGEPILOT_CONFIGURED_PYTHON%" call :try_runtime_python "%FORGEPILOT_CONFIGURED_PYTHON%"
if not defined FORGEPILOT_SELECTED_PYTHON if exist "%~dp0..\agent.venv\Scripts\python.exe" call :try_runtime_python "%~dp0..\agent.venv\Scripts\python.exe"
if not defined FORGEPILOT_SELECTED_PYTHON (
    echo ERROR: Python 3.12 runtime environment is unavailable. Run scripts\Setup-Agent.cmd first.
    exit /b 2
)
set "FORGEPILOT_PYTHON=%FORGEPILOT_SELECTED_PYTHON%"
if /i "%FORGEPILOT_ENV_MODE%"=="runtime" (
    echo ForgePilot runtime check passed. Python 3.12 is available.
    exit /b 0
)

:check_build
if not defined FORGEPILOT_VS_ROOT (
    echo ERROR: Required variable FORGEPILOT_VS_ROOT is not set in scripts\local-env.cmd.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_VS_ROOT%\" (
    echo ERROR: Required directory FORGEPILOT_VS_ROOT does not exist.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" (
    echo ERROR: Required Visual Studio environment script was not found under FORGEPILOT_VS_ROOT.
    set "FORGEPILOT_ENV_ERROR=1"
)

if not defined FORGEPILOT_QT_KIT (
    echo ERROR: Required variable FORGEPILOT_QT_KIT is not set in scripts\local-env.cmd.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_QT_KIT%\" (
    echo ERROR: Required directory FORGEPILOT_QT_KIT does not exist.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_QT_KIT%\bin\windeployqt.exe" (
    echo ERROR: Required Qt deployment tool was not found under FORGEPILOT_QT_KIT.
    set "FORGEPILOT_ENV_ERROR=1"
)

if not defined FORGEPILOT_CMAKE (
    echo ERROR: Required variable FORGEPILOT_CMAKE is not set in scripts\local-env.cmd.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_CMAKE%" (
    echo ERROR: Required executable FORGEPILOT_CMAKE does not exist.
    set "FORGEPILOT_ENV_ERROR=1"
)

if not "%FORGEPILOT_ENV_ERROR%"=="0" goto failed
if /i "%FORGEPILOT_ENV_MODE%"=="build" (
    echo ForgePilot client build check passed. MSVC, Qt, and CMake are available.
    exit /b 0
)
if /i "%FORGEPILOT_ENV_MODE%"=="all" goto check_uv

:check_client_test
if not defined FORGEPILOT_QT_KIT (
    echo ERROR: Required variable FORGEPILOT_QT_KIT is not set in scripts\local-env.cmd.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_QT_KIT%\bin\" (
    echo ERROR: Required Qt runtime directory was not found under FORGEPILOT_QT_KIT.
    set "FORGEPILOT_ENV_ERROR=1"
)
if not defined FORGEPILOT_CMAKE (
    echo ERROR: Required variable FORGEPILOT_CMAKE is not set in scripts\local-env.cmd.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_CMAKE%" (
    echo ERROR: Required executable FORGEPILOT_CMAKE does not exist.
    set "FORGEPILOT_ENV_ERROR=1"
)
if defined FORGEPILOT_CMAKE if exist "%FORGEPILOT_CMAKE%" for %%I in ("%FORGEPILOT_CMAKE%") do set "FORGEPILOT_CTEST=%%~dpIctest.exe"
if defined FORGEPILOT_CTEST if not exist "%FORGEPILOT_CTEST%" (
    echo ERROR: Required CTest executable was not found next to FORGEPILOT_CMAKE.
    set "FORGEPILOT_ENV_ERROR=1"
)
if not "%FORGEPILOT_ENV_ERROR%"=="0" goto failed
echo ForgePilot client test check passed. Qt and CTest are available.
exit /b 0

:check_uv
if not defined FORGEPILOT_UV (
    echo ERROR: Required variable FORGEPILOT_UV is not set in scripts\local-env.cmd.
    set "FORGEPILOT_ENV_ERROR=1"
) else if not exist "%FORGEPILOT_UV%" (
    echo ERROR: Required executable FORGEPILOT_UV does not exist.
    set "FORGEPILOT_ENV_ERROR=1"
)
for %%V in (NINJA RG CLANGD) do call :check_optional %%V
if not "%FORGEPILOT_ENV_ERROR%"=="0" goto failed
echo ForgePilot development environment check passed.
exit /b 0

:check_optional
call set "FORGEPILOT_OPTIONAL_VALUE=%%FORGEPILOT_%~1%%"
if not defined FORGEPILOT_OPTIONAL_VALUE (
    echo INFO: Optional tool FORGEPILOT_%~1 is not configured.
) else if not exist "%FORGEPILOT_OPTIONAL_VALUE%" (
    echo ERROR: Optional executable FORGEPILOT_%~1 is configured but does not exist.
    set "FORGEPILOT_ENV_ERROR=1"
)
exit /b 0

:try_runtime_python
if defined FORGEPILOT_SELECTED_PYTHON exit /b 0
"%~1" -c "import sys, fastapi, httpx, pydantic, uvicorn; raise SystemExit(sys.version_info[:2] != (3, 12))" >nul 2>nul
if not errorlevel 1 set "FORGEPILOT_SELECTED_PYTHON=%~1"
exit /b 0

:failed
echo ERROR: Environment check failed. Update scripts\local-env.cmd and run scripts\Check-Environment.cmd again.
exit /b 2
