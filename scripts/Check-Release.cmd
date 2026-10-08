@echo off
setlocal
call "%~dp0Check-Environment.cmd" runtime
if errorlevel 1 exit /b %errorlevel%

if not exist "%~dp0..\client\build\Release\ForgePilotClient.exe" (
    echo ERROR: Packaged ForgePilotClient.exe is missing.
    exit /b 2
)
if not exist "%~dp0..\client\build\Release\Qt6Core.dll" (
    echo ERROR: Packaged Qt runtime is incomplete.
    exit /b 2
)
if not exist "%~dp0..\client\build\Release\platforms\qwindows.dll" (
    echo ERROR: Packaged Qt platform plugin is missing.
    exit /b 2
)
if not exist "%~dp0release_smoke.py" (
    echo ERROR: Release health-check helper is missing.
    exit /b 2
)

"%FORGEPILOT_PYTHON%" -I "%~dp0release_smoke.py"
if errorlevel 1 (
    echo ERROR: Packaged backend health check failed.
    exit /b 1
)
echo ForgePilot release preflight passed. Client runtime and backend health are available.
exit /b 0
