@echo off
setlocal
call "%~dp0local-env.cmd"
if errorlevel 1 exit /b 1
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b 1
set "UV_PROJECT_ENVIRONMENT=%~dp0..\agent.venv"
set "UV_PYTHON=%FORGEPILOT_PYTHON%"
set "UV_PYTHON_DOWNLOADS=never"
set "UV_OFFLINE=true"
set "PYTHONIOENCODING=utf-8"
set "PATH=%FORGEPILOT_QT_KIT%\bin;%PATH%"
pushd "%~dp0..\agent"
"%FORGEPILOT_UV%" run --no-sync --locked python ..\client\tests\run_backend_integration.py %*
set "FORGEPILOT_TEST_EXIT=%errorlevel%"
popd
exit /b %FORGEPILOT_TEST_EXIT%
