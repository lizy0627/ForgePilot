@echo off
setlocal
call "%~dp0Check-Environment.cmd" all
if errorlevel 1 exit /b %errorlevel%
call "%FORGEPILOT_VS_ROOT%\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b %errorlevel%
set "PYTHONIOENCODING=utf-8"
"%~dp0..\agent.venv\Scripts\python.exe" "%~dp0..\eval\run_skill_pairs.py" %*
exit /b %errorlevel%
