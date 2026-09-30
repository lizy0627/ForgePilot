# Apply only to the current PowerShell process; do not modify PATH.
$env:UV_PROJECT_ENVIRONMENT = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\agent.venv'))
$env:UV_PYTHON = 'D:\Python312\python.exe'
$env:UV_PYTHON_DOWNLOADS = 'never'
