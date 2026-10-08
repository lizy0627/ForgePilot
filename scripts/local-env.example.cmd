@echo off
rem Copy this file to scripts\local-env.cmd, then replace every required placeholder.
rem Visual Studio installation root containing Common7\Tools\VsDevCmd.bat.
set "FORGEPILOT_VS_ROOT=C:\Path\To\Microsoft Visual Studio\2022\Edition"
rem Qt MSVC kit root containing bin\windeployqt.exe.
set "FORGEPILOT_QT_KIT=C:\Path\To\Qt\6.x.x\msvc2022_64"
rem CMake is required only for client builds, tests, and repository build tasks.
set "FORGEPILOT_CMAKE=C:\Path\To\CMake\bin\cmake.exe"
rem Optional Python override; leave empty to use the root agent.venv.
set "FORGEPILOT_PYTHON="
rem uv is used by setup and development workflows, not ordinary chat startup.
set "FORGEPILOT_UV=C:\Path\To\uv.exe"

rem Optional executable paths. Leave these empty when the tool is unavailable.
set "FORGEPILOT_NINJA=C:\Path\To\Ninja\ninja.exe"
set "FORGEPILOT_RG=C:\Path\To\ripgrep\rg.exe"
set "FORGEPILOT_CLANGD=C:\Path\To\LLVM\bin\clangd.exe"
