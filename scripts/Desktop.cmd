@echo off
setlocal
set "FORGEPILOT_DESKTOP_EXIT=1"
for /f "tokens=2 delims=:" %%p in ('chcp') do set "FORGEPILOT_DESKTOP_OLD_CP=%%p"
chcp 65001 >nul
title ForgePilot - %~1
if /i "%~1"=="Stop" goto run
if /i "%~1"=="Check" goto run
if /i "%~1"=="Configure" goto run
if not exist "%~dp0local-env.cmd" (
    echo Missing scripts\local-env.cmd. See docs\environment.md.
    goto failed
)
call "%~dp0local-env.cmd"
if errorlevel 1 goto failed
if not exist "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" (
    echo Missing MSVC environment script. Check FORGEPILOT_VS_ROOT in scripts\local-env.cmd.
    goto failed
)
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 goto failed
:run
rem Let Windows PowerShell use its own modules even when called from PowerShell 7.
set "PSModulePath="
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Desktop.ps1" -Action "%~1"
set "FORGEPILOT_DESKTOP_EXIT=%errorlevel%"
if not "%FORGEPILOT_DESKTOP_EXIT%"=="0" goto failed
if /i "%~1"=="Check" pause
if /i "%~1"=="Configure" pause
goto finish
:failed
if "%FORGEPILOT_DESKTOP_EXIT%"=="0" set "FORGEPILOT_DESKTOP_EXIT=1"
echo ForgePilot launcher failed. See the message above.
pause
:finish
if defined FORGEPILOT_DESKTOP_OLD_CP chcp %FORGEPILOT_DESKTOP_OLD_CP% >nul
exit /b %FORGEPILOT_DESKTOP_EXIT%
