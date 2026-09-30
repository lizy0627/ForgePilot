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
set "UV_OFFLINE=true"
if not defined FORGEPILOT_TEST_CMAKE set "FORGEPILOT_TEST_CMAKE=%FORGEPILOT_CMAKE%"
if not defined FORGEPILOT_TEST_NINJA set "FORGEPILOT_TEST_NINJA=%FORGEPILOT_NINJA%"
if not defined FORGEPILOT_TEST_CLANGD set "FORGEPILOT_TEST_CLANGD=%FORGEPILOT_CLANGD%"
pushd "%~dp0..\agent"
"%FORGEPILOT_UV%" run --no-sync --locked pytest %*
set "FORGEPILOT_TEST_EXIT=%errorlevel%"
popd
exit /b %FORGEPILOT_TEST_EXIT%
