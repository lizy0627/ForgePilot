@echo off
setlocal
call "%~dp0Check-Environment.cmd" client-test
if errorlevel 1 exit /b 1
set "PATH=%FORGEPILOT_QT_KIT%\bin;%PATH%"
"%FORGEPILOT_CTEST%" --test-dir "%~dp0..\client\build" -C Release --output-on-failure %*
set "FORGEPILOT_TEST_EXIT=%errorlevel%"
if exist "%~dp0..\client\build\communication-results.txt" type "%~dp0..\client\build\communication-results.txt"
for %%s in (mission_api mission_sse workbench_models mission_controller) do if exist "%~dp0..\client\build\%%s-results.txt" type "%~dp0..\client\build\%%s-results.txt"
exit /b %FORGEPILOT_TEST_EXIT%
