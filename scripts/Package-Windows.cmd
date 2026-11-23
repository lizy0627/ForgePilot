@echo off
setlocal
rem Let Windows PowerShell use its own modules even when called from PowerShell 7.
set "PSModulePath="
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Package-Windows.ps1" %*
exit /b %errorlevel%
