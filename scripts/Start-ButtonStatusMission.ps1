param([string]$ArtifactRoot = 'D:/FPDemo/artifacts')
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($ArtifactRoot)
if (-not [IO.Path]::IsPathRooted($root) -or $root.Length -gt 80) {
    throw 'ArtifactRoot must be a short absolute path'
}
$env:FORGEPILOT_ARTIFACT_ROOT = $root
& (Join-Path $PSScriptRoot 'Desktop.ps1') -Action Start
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
