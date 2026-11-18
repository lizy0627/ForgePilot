@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Package-Windows.ps1" %*
exit /b %errorlevel%
