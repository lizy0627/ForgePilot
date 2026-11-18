param(
    [Parameter(Mandatory=$true)][string]$Repository,
    [Parameter(Mandatory=$true)][string]$LauncherConfig,
    [string]$ArtifactRoot = $env:FORGEPILOT_ARTIFACT_ROOT
)
$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path -LiteralPath $Repository).Path
$config = Get-Content -LiteralPath $LauncherConfig -Raw | ConvertFrom-Json
$arguments = @($config.server_args)
function Argument-Value([string]$name) {
    $index = [array]::IndexOf($arguments, $name)
    if ($index -lt 0 -or $index + 1 -ge $arguments.Count) { throw "Missing $name" }
    return $arguments[$index + 1]
}
$modelConfig = Argument-Value '--model-config'
$oracle = Argument-Value '--mission-oracle'
$expectedOracle = (Resolve-Path (Join-Path $PSScriptRoot '../eval/demo/qt-button-status-oracle')).Path
if (-not (Test-Path -LiteralPath $modelConfig -PathType Leaf)) { throw 'Model configuration is missing' }
if (-not (Test-Path -LiteralPath $oracle -PathType Container)) { throw 'Oracle is missing' }
if (-not [IO.Path]::GetFullPath($oracle).Equals($expectedOracle, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Mission must use the button status demo Oracle'
}
if (-not ($arguments -contains '--mission-allow-cloud-model')) { throw 'Cloud model permission is missing' }
if ($arguments -contains '--configure-preset' -or $arguments -contains '--build-preset' -or
    $arguments -contains '--test-preset') { throw 'Mission API v2 does not support CMake presets' }
if (-not (Test-Path -LiteralPath (Join-Path $repo '.git'))) { throw 'Demo must be an independent Git repository' }
$autocrlf = git -C $repo config --get core.autocrlf
if ($LASTEXITCODE -ne 0 -or $autocrlf -ne 'false') {
    throw 'Set git core.autocrlf=false in this demo repository before creating Mission worktrees; the Oracle requires byte-identical CMake files'
}
$selected = (Get-FileHash -LiteralPath (Join-Path $repo 'CMakeLists.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
$expected = (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot '../eval/demo/qt-button-status/CMakeLists.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
if ($selected -ne $expected) { throw 'Demo CMake identity does not match the Oracle binding' }
if (-not ((Get-Content -LiteralPath (Join-Path $oracle 'CMakeLists.txt') -Raw).Contains($expected))) {
    throw 'Oracle identity binding does not match the demo CMake definition'
}
$kitFile = Join-Path $repo 'cmake/QtKit.cmake'
$oracleKitFile = Join-Path $oracle 'QtKit.cmake'
if (-not (Test-Path -LiteralPath $kitFile -PathType Leaf) -or
    -not (Test-Path -LiteralPath $oracleKitFile -PathType Leaf)) {
    throw 'Both demo and Oracle need a trusted QtKit.cmake'
}
if (-not $ArtifactRoot -or -not [IO.Path]::IsPathRooted($ArtifactRoot) -or
    [IO.Path]::GetFullPath($ArtifactRoot).Length -gt 80) {
    throw 'Set FORGEPILOT_ARTIFACT_ROOT to a short absolute path before launching ForgePilot'
}
if ([IO.Path]::GetFullPath($oracle).StartsWith($repo + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Oracle must be outside the demo repository'
}
$python = Join-Path $PSScriptRoot '../agent.venv/Scripts/python.exe'
$env:PYTHONPATH = (Resolve-Path (Join-Path $PSScriptRoot '../agent')).Path
$env:FORGEPILOT_DEMO_MODEL_CONFIG = $modelConfig
try {
    & $python (Join-Path $PSScriptRoot '../eval/demo/check_button_status_routes.py')
    if ($LASTEXITCODE -ne 0) { throw 'Mission model routes are invalid' }
} finally {
    Remove-Item Env:FORGEPILOT_DEMO_MODEL_CONFIG -ErrorAction SilentlyContinue
}
Write-Output 'Button status Mission configuration is ready.'
