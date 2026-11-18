@echo off
setlocal
if exist "%~dp0..\bin\ForgePilotAgent.exe" goto check_client
if not exist "%~dp0Check-Environment.cmd" (
    echo ERROR: Self-contained backend executable is missing.
    exit /b 2
)
call "%~dp0Check-Environment.cmd" runtime
if errorlevel 1 exit /b %errorlevel%
if not exist "%~dp0release_smoke.py" (
    echo ERROR: Release health-check helper is missing.
    exit /b 2
)

:check_client
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
if exist "%~dp0..\bin\ForgePilotAgent.exe" (
    if not exist "%~dp0..\client\build\Release\platforms\qoffscreen.dll" (
        echo ERROR: Packaged Qt offscreen plugin is missing.
        exit /b 2
    )
    set "QT_QPA_PLATFORM=offscreen"
    start "" /wait "%~dp0..\client\build\Release\ForgePilotClient.exe" --self-test --server http://127.0.0.1:9
    if errorlevel 1 (
        echo ERROR: Packaged Qt/QML client failed to load.
        exit /b 1
    )
)
if exist "%~dp0..\bin\ForgePilotAgent.exe" (
    "%~dp0..\bin\ForgePilotAgent.exe" smoke
) else (
    "%FORGEPILOT_PYTHON%" -I "%~dp0release_smoke.py"
)
if errorlevel 1 (
    echo ERROR: Packaged backend health check failed.
    exit /b 1
)
echo ForgePilot release preflight passed. Client runtime and backend health are available.
exit /b 0
