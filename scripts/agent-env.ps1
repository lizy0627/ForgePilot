# Apply only to the current PowerShell process; do not modify PATH.
$environmentRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\agent.venv'))
$environmentPython = Join-Path $environmentRoot 'Scripts\python.exe'
$configuredPython = $env:FORGEPILOT_PYTHON

if ($configuredPython) {
    if (-not (Test-Path -LiteralPath $configuredPython -PathType Leaf)) {
        throw 'Configured FORGEPILOT_PYTHON does not exist.'
    }
    $selectedPython = $configuredPython
} elseif (Test-Path -LiteralPath $environmentPython -PathType Leaf) {
    $selectedPython = $environmentPython
} else {
    $pathPython = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pathPython) { throw 'Python 3.12 was not found on PATH.' }
    $selectedPython = $pathPython.Source
}

& $selectedPython -c 'import sys; raise SystemExit(sys.version_info[:2] != (3, 12))' 2>$null
if ($LASTEXITCODE -ne 0) { throw 'Selected Python must be Python 3.12.' }

$env:FORGEPILOT_PYTHON = $selectedPython
$env:UV_PROJECT_ENVIRONMENT = $environmentRoot
$env:UV_PYTHON = $selectedPython
$env:UV_PYTHON_DOWNLOADS = 'never'
