@echo off
setlocal
if not exist "%~dp0local-env.cmd" (
    echo Missing scripts\local-env.cmd. See docs\environment.md.
    exit /b 1
)
call "%~dp0local-env.cmd"
if errorlevel 1 exit /b %errorlevel%
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b %errorlevel%
if not exist "%~dp0..\agent.venv\Scripts\python.exe" (
    echo Missing existing root agent.venv. No environment was created.
    exit /b 1
)
set "UV_PROJECT_ENVIRONMENT=%~dp0..\agent.venv"
set "UV_PYTHON=%FORGEPILOT_PYTHON%"
set "UV_PYTHON_DOWNLOADS=never"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0..\agent"
"%FORGEPILOT_UV%" run --no-sync --locked python ..\eval\run.py %*
set "FORGEPILOT_EVAL_EXIT=%errorlevel%"
popd
exit /b %FORGEPILOT_EVAL_EXIT%
