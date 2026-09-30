@echo off
setlocal
call "%~dp0local-env.cmd"
if errorlevel 1 exit /b 1
set "PATH=%FORGEPILOT_QT_KIT%\bin;%PATH%"
for %%I in ("%FORGEPILOT_CMAKE%") do set "FORGEPILOT_CMAKE_BIN=%%~dpI"
"%FORGEPILOT_CMAKE_BIN%ctest.exe" --test-dir "%~dp0..\client\build" -C Release --output-on-failure %*
set "FORGEPILOT_TEST_EXIT=%errorlevel%"
if exist "%~dp0..\client\build\communication-results.txt" type "%~dp0..\client\build\communication-results.txt"
exit /b %FORGEPILOT_TEST_EXIT%
