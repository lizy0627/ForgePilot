@echo off
setlocal
if not exist "%~dp0local-env.cmd" exit /b 2
call "%~dp0local-env.cmd"
if errorlevel 1 exit /b %errorlevel%
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b %errorlevel%
if not exist "%~dp0..\agent.venv\Scripts\python.exe" exit /b 2
set "UV_PROJECT_ENVIRONMENT=%~dp0..\agent.venv"
set "UV_PYTHON=%FORGEPILOT_PYTHON%"
set "UV_PYTHON_DOWNLOADS=never"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0..\agent"
"%FORGEPILOT_UV%" run --no-sync --locked python ..\scripts\prepare_demo.py %*
set "FORGEPILOT_DEMO_EXIT=%errorlevel%"
popd
exit /b %FORGEPILOT_DEMO_EXIT%
