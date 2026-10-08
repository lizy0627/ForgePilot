@echo off
setlocal
call "%~dp0Check-Environment.cmd" runtime
if errorlevel 1 exit /b %errorlevel%
if exist "%~dp0..\agent\.venv" echo WARNING: agent\.venv is not supported. Use the root agent.venv; the old environment was not removed.
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0..\agent"
"%FORGEPILOT_PYTHON%" -m forgepilot.server %*
set "FORGEPILOT_SERVER_EXIT=%errorlevel%"
popd
exit /b %FORGEPILOT_SERVER_EXIT%
