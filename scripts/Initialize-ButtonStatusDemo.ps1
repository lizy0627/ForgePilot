param([Parameter(Mandatory=$true)][string]$Destination,
      [Parameter(Mandatory=$true)][string]$QtKitRoot)
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path (Join-Path $PSScriptRoot '../eval/demo/qt-button-status')).Path
$target = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $target) { throw "Destination already exists: $target" }
New-Item -ItemType Directory -Path $target | Out-Null
Copy-Item -LiteralPath (Join-Path $source 'CMakeLists.txt'),(Join-Path $source 'PROJECT.txt') -Destination $target
foreach ($name in 'src','tests','cmake') {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination $target -Recurse
}
$qt = [IO.Path]::GetFullPath($QtKitRoot)
if (-not (Test-Path -LiteralPath (Join-Path $qt 'lib/cmake/Qt6/Qt6Config.cmake'))) {
    throw 'QtKitRoot must contain lib/cmake/Qt6/Qt6Config.cmake'
}
$cmakePath = $qt.Replace('\','/')
Set-Content -LiteralPath (Join-Path $target 'cmake/QtKit.cmake') -Value "list(PREPEND CMAKE_PREFIX_PATH `"$cmakePath`")"
git -C $target init -q
if ($LASTEXITCODE -ne 0) { throw 'git init failed' }
# The Oracle binds the exact CMake bytes; task worktrees must preserve them.
git -C $target config core.autocrlf false
if ($LASTEXITCODE -ne 0) { throw 'git core.autocrlf configuration failed' }
git -C $target add .
if ($LASTEXITCODE -ne 0) { throw 'git add failed' }
git -C $target -c user.name='ForgePilot Demo' -c user.email='demo@localhost' commit -qm 'Baseline button status demo with failing click test'
if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
Write-Output "Demo repository: $target"
Write-Output "Baseline: $(git -C $target rev-parse HEAD)"
