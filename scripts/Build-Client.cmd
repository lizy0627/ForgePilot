@echo off
setlocal
set "FORGEPILOT_BUILD_EXIT=1"
for /f "tokens=2 delims=:" %%p in ('chcp') do set "FORGEPILOT_OLD_CP=%%p"
call "%~dp0Check-Environment.cmd" build
if errorlevel 1 goto finish
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64
if errorlevel 1 (
    echo ERROR: Visual Studio build environment could not be initialized.
    goto finish
)
if not defined VSCMD_VER (
    echo ERROR: Visual Studio build environment was not initialized.
    goto finish
)
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
