@echo off
setlocal
set "FORGEPILOT_DESKTOP_EXIT=1"
for /f "tokens=2 delims=:" %%p in ('chcp') do set "FORGEPILOT_DESKTOP_OLD_CP=%%p"
chcp 65001 >nul
title ForgePilot - %~1
if not exist "%~dp0..\bin\ForgePilotAgent.exe" (
    call "%~dp0Check-Environment.cmd" runtime
    if errorlevel 1 goto failed
)
:run
rem Let Windows PowerShell use its own modules even when called from PowerShell 7.
set "PSModulePath="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Desktop.ps1" -Action "%~1"
set "FORGEPILOT_DESKTOP_EXIT=%errorlevel%"
if not "%FORGEPILOT_DESKTOP_EXIT%"=="0" goto failed
if /i "%~1"=="Check" pause
goto finish
:failed
if "%FORGEPILOT_DESKTOP_EXIT%"=="0" set "FORGEPILOT_DESKTOP_EXIT=1"
echo ForgePilot launcher failed. See the message above.
pause
:finish
if defined FORGEPILOT_DESKTOP_OLD_CP chcp %FORGEPILOT_DESKTOP_OLD_CP% >nul
exit /b %FORGEPILOT_DESKTOP_EXIT%
