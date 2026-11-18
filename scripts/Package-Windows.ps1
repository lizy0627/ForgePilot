param(
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]*$')]
    [string]$Version = 'local',
    [string]$OutputDirectory,
    [switch]$Verify
)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $root 'artifacts' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
$packageName = "ForgePilot-$Version-windows-x64"
$stage = Join-Path $output ('.forgepilot-stage-' + [Guid]::NewGuid().ToString('N'))
$zip = Join-Path $output ($packageName + '.zip')
$checksum = $zip + '.sha256'
$buildRoot = Join-Path $output ('.forgepilot-freeze-' + [Guid]::NewGuid().ToString('N'))

function Assert-UnderOutput([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = $output.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Packaging target is outside the selected output directory.'
    }
}

function Assert-NoReparseInPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw 'Packaging path contains a junction or symbolic link.'
            }
        }
        $parent = Split-Path -Parent $current
        if (-not $parent -or $parent -eq $current) { break }
        $current = $parent
    }
}

function Copy-File([string]$Source, [string]$RelativeDestination) {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required release input is missing: $RelativeDestination"
    }
    $destination = Join-Path $stage $RelativeDestination
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $destination -Force
}

function Get-RelativeFilePath([string]$Base, [string]$Target) {
    $basePath = [IO.Path]::GetFullPath($Base).TrimEnd('\') + '\'
    $baseUri = [Uri]::new($basePath)
    $targetUri = [Uri]::new([IO.Path]::GetFullPath($Target))
    return [Uri]::UnescapeDataString($baseUri.MakeRelativeUri($targetUri).ToString()).Replace('/', '\')
}

Assert-NoReparseInPath $output
New-Item -ItemType Directory -Path $output -Force | Out-Null
Assert-NoReparseInPath $output
Assert-UnderOutput $stage
Assert-UnderOutput $zip
Assert-UnderOutput $checksum
Assert-UnderOutput $buildRoot
foreach ($path in @($stage, $zip, $checksum, $buildRoot)) { Assert-NoReparseInPath $path }

$python = Join-Path $root 'agent.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Build requires the locked root agent.venv Python 3.12 environment.'
}
& $python -c 'import sys, fastapi, uvicorn, mcp, qdrant_client; raise SystemExit(sys.version_info[:2] != (3, 12))'
if ($LASTEXITCODE -ne 0) { throw 'Build requires a complete Python 3.12 agent environment.' }
$uv = Get-Command uv.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $uv) { throw 'Build host requires uv.exe; the resulting ZIP does not.' }
$previousProjectEnvironment = $env:UV_PROJECT_ENVIRONMENT
$previousPythonPath = $env:PYTHONPATH
try {
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $root 'agent.venv'
    $env:PYTHONPATH = Join-Path $root 'agent'
    $pyinstallerArgs = @(
        'run', '--project', (Join-Path $root 'agent'), '--no-sync',
        '--with', 'pyinstaller==6.22.3', 'python', '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onedir', '--contents-directory', '_internal',
        '--name', 'ForgePilotAgent',
        '--paths', (Join-Path $root 'agent'), '--paths', $PSScriptRoot,
        '--collect-submodules', 'forgepilot', '--collect-submodules', 'uvicorn',
        '--add-data', ((Join-Path $root 'agent\forgepilot\protocol_examples.v1.json') + ':forgepilot'),
        '--collect-submodules', 'qdrant_client',
        '--distpath', (Join-Path $buildRoot 'dist'),
        '--workpath', (Join-Path $buildRoot 'work'),
        '--specpath', (Join-Path $buildRoot 'spec'),
        (Join-Path $PSScriptRoot 'desktop_launcher.py')
    )
    & $uv.Source @pyinstallerArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} catch {
    if (Test-Path -LiteralPath $buildRoot) {
        Assert-NoReparseInPath $buildRoot
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }
    throw
} finally {
    if ($null -eq $previousProjectEnvironment) { Remove-Item Env:UV_PROJECT_ENVIRONMENT -ErrorAction SilentlyContinue }
    else { $env:UV_PROJECT_ENVIRONMENT = $previousProjectEnvironment }
    if ($null -eq $previousPythonPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    else { $env:PYTHONPATH = $previousPythonPath }
}
$frozen = Join-Path $buildRoot 'dist\ForgePilotAgent'
if (-not (Test-Path -LiteralPath (Join-Path $frozen 'ForgePilotAgent.exe') -PathType Leaf)) {
    throw 'PyInstaller did not produce ForgePilotAgent.exe.'
}
if (Test-Path -LiteralPath $stage) { throw 'Unique release staging path already exists.' }
foreach ($path in @($zip, $checksum, $buildRoot)) { Assert-NoReparseInPath $path }
New-Item -ItemType Directory -Path $stage | Out-Null

$releaseReadme = Join-Path $root 'docs\windows-release-readme.md'
if (Test-Path -LiteralPath $releaseReadme -PathType Leaf) {
    Copy-File $releaseReadme 'README.md'
}
foreach ($name in @('Start-ForgePilot.cmd', 'Stop-ForgePilot.cmd', 'Configure-ForgePilot.cmd', 'Check-Model.cmd')) {
    Copy-File (Join-Path $root $name) $name
}
foreach ($name in @('Desktop.cmd', 'Desktop.ps1', 'Check-Release.cmd')) {
    Copy-File (Join-Path $PSScriptRoot $name) "scripts\$name"
}
Get-ChildItem -LiteralPath $frozen -Recurse -File | Sort-Object FullName | ForEach-Object {
    $relative = Get-RelativeFilePath $frozen $_.FullName
    Copy-File $_.FullName (Join-Path 'bin' $relative)
}

$clientSource = Join-Path $root 'client\build\Release'
Copy-File (Join-Path $clientSource 'ForgePilotClient.exe') 'client\build\Release\ForgePilotClient.exe'
Get-ChildItem -LiteralPath $clientSource -File | Where-Object {
    $_.Extension -eq '.dll' -or $_.Name -eq 'vc_redist.x64.exe'
} | Sort-Object Name | ForEach-Object {
    Copy-File $_.FullName ("client\build\Release\" + $_.Name)
}
Get-ChildItem -LiteralPath $clientSource -Directory | Sort-Object Name | ForEach-Object {
    $directory = $_
    Get-ChildItem -LiteralPath $directory.FullName -Recurse -File | Sort-Object FullName | ForEach-Object {
        $relative = Get-RelativeFilePath $clientSource $_.FullName
        Copy-File $_.FullName (Join-Path 'client\build\Release' $relative)
    }
}

$relativeFiles = Get-ChildItem -LiteralPath $stage -Recurse -File | ForEach-Object {
    (Get-RelativeFilePath $stage $_.FullName).Replace('\', '/')
}
$forbiddenNames = '(?i)(^|/)(config\.json|api-key\.dpapi|logs?|__pycache__|\.pytest_cache|uv\.exe|pythonw?\.exe)(/|$)|\.(py|db|sqlite3?|log|pyc|pdb|obj|lib|exp)$'
$badNames = @($relativeFiles | Where-Object { $_ -match $forbiddenNames })
if ($badNames.Count) { throw 'Release staging contains forbidden runtime, secret, log, cache, or build files.' }

$needles = @(
    $root,
    $root.Replace('\', '/'),
    [Environment]::GetFolderPath('UserProfile'),
    [Environment]::GetFolderPath('UserProfile').Replace('\', '/')
) | Where-Object { $_ } | Sort-Object -Unique
foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File | Where-Object {
    $_.Extension -in @('.cmd', '.ps1', '.md', '.json', '.txt', '.toml', '.xml')
}) {
    $bytes = [IO.File]::ReadAllBytes($file.FullName)
    $singleByteText = [Text.Encoding]::GetEncoding(28591).GetString($bytes)
    $unicodeText = [Text.Encoding]::Unicode.GetString($bytes)
    foreach ($needle in $needles) {
        if ($singleByteText.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0 -or
            $unicodeText.IndexOf($needle, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            throw 'Release staging contains a developer-machine absolute path.'
        }
    }
}

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
function New-ReleaseArchive([string]$ArchivePath) {
    $archive = [IO.Compression.ZipFile]::Open($ArchivePath, [IO.Compression.ZipArchiveMode]::Create)
    try {
        Get-ChildItem -LiteralPath $stage -Recurse -File | Sort-Object FullName | ForEach-Object {
            $relative = (Get-RelativeFilePath $stage $_.FullName).Replace('\', '/')
            $entry = $archive.CreateEntry("$packageName/$relative", [IO.Compression.CompressionLevel]::Optimal)
            $entry.LastWriteTime = [DateTimeOffset]::new(1980, 1, 1, 0, 0, 0, [TimeSpan]::Zero)
            $input = [IO.File]::OpenRead($_.FullName)
            $outputStream = $entry.Open()
            try { $input.CopyTo($outputStream) } finally { $outputStream.Dispose(); $input.Dispose() }
        }
    } finally { $archive.Dispose() }
}

function Test-ExtractedRelease([string]$ArchivePath) {
    $temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    # Keep extraction under MAX_PATH even when a Qt QML asset has a long name.
    $verifyRoot = Join-Path $temporaryRoot ('fpv-' + [Guid]::NewGuid().ToString('N').Substring(0, 12))
    $verifyRoot = [IO.Path]::GetFullPath($verifyRoot)
    if (-not $verifyRoot.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Release verification target is outside the temporary directory.'
    }
    $previousPath = $env:PATH
    $previousPython = $env:FORGEPILOT_PYTHON
    $previousLocalAppData = $env:LOCALAPPDATA
    try {
        [IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $verifyRoot)
        $extracted = Join-Path $verifyRoot $packageName
        $windows = [Environment]::GetFolderPath('Windows')
        $env:PATH = (Join-Path $windows 'System32') + ';' + (Join-Path $windows 'System32\WindowsPowerShell\v1.0')
        Remove-Item Env:FORGEPILOT_PYTHON -ErrorAction SilentlyContinue
        $env:LOCALAPPDATA = Join-Path $verifyRoot 'local-app-data'
        if (Get-Command python.exe, uv.exe -CommandType Application -ErrorAction SilentlyContinue) {
            throw 'Verification PATH unexpectedly contains Python or uv.'
        }
        if (Test-Path -LiteralPath (Join-Path $extracted 'agent')) {
            throw 'Self-contained release unexpectedly contains agent source.'
        }
        Push-Location $extracted
        try {
            & '.\scripts\Check-Release.cmd'
            if ($LASTEXITCODE -ne 0) { throw 'Extracted release preflight failed.' }
            & '.\Stop-ForgePilot.cmd'
            if ($LASTEXITCODE -ne 0) { throw 'Extracted release stop entrypoint failed.' }
        } finally { Pop-Location }
    } finally {
        $env:PATH = $previousPath
        if ($null -eq $previousPython) { Remove-Item Env:FORGEPILOT_PYTHON -ErrorAction SilentlyContinue }
        else { $env:FORGEPILOT_PYTHON = $previousPython }
        if ($null -eq $previousLocalAppData) { Remove-Item Env:LOCALAPPDATA -ErrorAction SilentlyContinue }
        else { $env:LOCALAPPDATA = $previousLocalAppData }
        if (Test-Path -LiteralPath $verifyRoot) {
            Assert-NoReparseInPath $verifyRoot
            Remove-Item -LiteralPath $verifyRoot -Recurse -Force
        }
    }
}

$pendingZip = Join-Path $output ('.forgepilot-' + [Guid]::NewGuid().ToString('N') + '.zip')
$pendingChecksum = $pendingZip + '.sha256'
$backupZip = $pendingZip + '.previous'
try {
    try { New-ReleaseArchive $pendingZip } finally {
        Assert-NoReparseInPath $stage
        Assert-NoReparseInPath $buildRoot
        Remove-Item -LiteralPath $stage -Recurse -Force
        Remove-Item -LiteralPath $buildRoot -Recurse -Force
    }
    $archive = [IO.Compression.ZipFile]::OpenRead($pendingZip)
    try {
        if ($archive.Entries.Count -ne @($relativeFiles).Count) { throw 'Release ZIP entry count mismatch.' }
    } finally { $archive.Dispose() }

    $hash = (Get-FileHash -LiteralPath $pendingZip -Algorithm SHA256).Hash.ToLowerInvariant()
    $zipName = [IO.Path]::GetFileName($zip)
    [IO.File]::WriteAllText($pendingChecksum, "$hash *$zipName`n", [Text.UTF8Encoding]::new($false))
    if ($Verify) { Test-ExtractedRelease $pendingZip }
    $verifiedHash = (Get-FileHash -LiteralPath $pendingZip -Algorithm SHA256).Hash.ToLowerInvariant()
    if ([IO.File]::ReadAllText($pendingChecksum) -cne "$verifiedHash *$zipName`n") {
        throw 'Release ZIP checksum mismatch.'
    }

    # Serialize the two final renames for publishers of the same version.
    $lock = [IO.FileStream]::new("$zip.publish.lock", [IO.FileMode]::CreateNew,
        [IO.FileAccess]::Write, [IO.FileShare]::None, 1, [IO.FileOptions]::DeleteOnClose)
    try {
        if ([IO.File]::Exists($zip)) { [IO.File]::Replace($pendingZip, $zip, $backupZip) }
        else { [IO.File]::Move($pendingZip, $zip) }
        try {
            if ([IO.File]::Exists($checksum)) {
                [IO.File]::Replace($pendingChecksum, $checksum, [Management.Automation.Language.NullString]::Value)
            } else { [IO.File]::Move($pendingChecksum, $checksum) }
        } catch {
            # The old checksum is still published. Restore its ZIP before releasing the lock.
            if ([IO.File]::Exists($backupZip)) { [IO.File]::Replace($backupZip, $zip, $pendingZip) }
            else { [IO.File]::Delete($zip) }
            throw
        }
        [IO.File]::Delete($backupZip)
    } finally { $lock.Dispose() }
} finally {
    [IO.File]::Delete($pendingZip)
    [IO.File]::Delete($pendingChecksum)
}

$size = (Get-Item -LiteralPath $zip).Length
Write-Output "Release artifact: $zip"
Write-Output "SHA-256: $hash"
Write-Output "Bytes: $size"
