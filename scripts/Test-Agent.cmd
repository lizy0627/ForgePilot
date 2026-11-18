@echo off
setlocal
call "%~dp0Check-Environment.cmd" runtime
if errorlevel 1 exit /b %errorlevel%
if exist "%~dp0..\agent\.venv" echo WARNING: agent\.venv is not supported. Use the root agent.venv; the old environment was not removed.
if not exist "%~dp0..\agent.venv\Scripts\python.exe" (
    echo Missing root agent.venv. Run scripts\Setup-Agent.cmd first.
    exit /b 2
)
set "PYTHONIOENCODING=utf-8"
set "UV_OFFLINE=true"
if not defined FORGEPILOT_TEST_CMAKE set "FORGEPILOT_TEST_CMAKE=%FORGEPILOT_CMAKE%"
if not defined FORGEPILOT_TEST_NINJA set "FORGEPILOT_TEST_NINJA=%FORGEPILOT_NINJA%"
if not defined FORGEPILOT_TEST_CLANGD set "FORGEPILOT_TEST_CLANGD=%FORGEPILOT_CLANGD%"
pushd "%~dp0..\agent"
"%FORGEPILOT_PYTHON%" -m pytest %*
set "FORGEPILOT_TEST_EXIT=%errorlevel%"
popd
exit /b %FORGEPILOT_TEST_EXIT%
