@echo off
setlocal
set "FORGEPILOT_BUILD_EXIT=1"
for /f "tokens=2 delims=:" %%p in ('chcp') do set "FORGEPILOT_OLD_CP=%%p"
if not exist "%~dp0local-env.cmd" (
    echo Missing scripts\local-env.cmd. See docs\environment.md.
    exit /b 1
)
call "%~dp0local-env.cmd"
if errorlevel 1 goto finish
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64
if errorlevel 1 goto finish
chcp 65001 >nul
pushd "%~dp0..\client"
set "FORGEPILOT_BUILD_PUSHED=1"
"%FORGEPILOT_CMAKE%" --preset msvc2022-x64
if errorlevel 1 goto finish
"%FORGEPILOT_CMAKE%" --build --preset release
if errorlevel 1 goto finish
"%FORGEPILOT_QT_KIT%\bin\windeployqt.exe" --release --no-translations --qmldir "%CD%" "build\Release\ForgePilotClient.exe"
if errorlevel 1 goto finish
set "FORGEPILOT_BUILD_EXIT=0"
:finish
if defined FORGEPILOT_BUILD_PUSHED popd
if defined FORGEPILOT_OLD_CP chcp %FORGEPILOT_OLD_CP% >nul
exit /b %FORGEPILOT_BUILD_EXIT%
