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
$stage = Join-Path $output $packageName
$zip = Join-Path $output ($packageName + '.zip')
$checksum = $zip + '.sha256'

function Assert-UnderOutput([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = $output.TrimEnd('\') + '\'
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Packaging target is outside the selected output directory.'
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

function Copy-PythonTree([string]$Source, [string]$RelativeDestination) {
    Get-ChildItem -LiteralPath $Source -Recurse -File -Filter '*.py' | Sort-Object FullName | ForEach-Object {
        $relative = Get-RelativeFilePath $Source $_.FullName
        Copy-File $_.FullName (Join-Path $RelativeDestination $relative)
    }
}

New-Item -ItemType Directory -Path $output -Force | Out-Null
Assert-UnderOutput $stage
Assert-UnderOutput $zip
Assert-UnderOutput $checksum
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Path $stage | Out-Null

foreach ($name in @('README.md', 'Start-ForgePilot.cmd', 'Stop-ForgePilot.cmd', 'Configure-ForgePilot.cmd', 'Check-Model.cmd')) {
    Copy-File (Join-Path $root $name) $name
}
foreach ($name in @('pyproject.toml', 'uv.lock', '.python-version')) {
    Copy-File (Join-Path $root "agent\$name") "agent\$name"
}
Copy-PythonTree (Join-Path $root 'agent\forgepilot') 'agent\forgepilot'
Copy-File (Join-Path $root 'agent\forgepilot\protocol_examples.v1.json') 'agent\forgepilot\protocol_examples.v1.json'

foreach ($name in @(
    'Desktop.cmd', 'Desktop.ps1', 'desktop_launcher.py', 'Setup-Agent.cmd',
    'Check-Environment.cmd', 'Start-Agent.cmd', 'Check-Release.cmd',
    'release_smoke.py', 'local-env.example.cmd'
)) {
    Copy-File (Join-Path $PSScriptRoot $name) "scripts\$name"
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
$forbiddenNames = '(?i)(^|/)(config\.json|api-key\.dpapi|logs?|__pycache__|\.pytest_cache)(/|$)|\.(db|sqlite3?|log|pyc|pdb|obj|lib|exp)$'
$badNames = @($relativeFiles | Where-Object { $_ -match $forbiddenNames })
if ($badNames.Count) { throw 'Release staging contains forbidden runtime, secret, log, cache, or build files.' }

$needles = @(
    $root,
    $root.Replace('\', '/'),
    [Environment]::GetFolderPath('UserProfile'),
    [Environment]::GetFolderPath('UserProfile').Replace('\', '/')
) | Where-Object { $_ } | Sort-Object -Unique
foreach ($file in Get-ChildItem -LiteralPath $stage -Recurse -File) {
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
    $verifyRoot = Join-Path $temporaryRoot ('ForgePilot-release-verify-' + [Guid]::NewGuid().ToString('N'))
    $verifyRoot = [IO.Path]::GetFullPath($verifyRoot)
    if (-not $verifyRoot.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Release verification target is outside the temporary directory.'
    }
    $previousPython = $env:FORGEPILOT_PYTHON
    try {
        [IO.Compression.ZipFile]::ExtractToDirectory($ArchivePath, $verifyRoot)
        $extracted = Join-Path $verifyRoot $packageName
        if (-not $env:FORGEPILOT_PYTHON -or -not (Test-Path -LiteralPath $env:FORGEPILOT_PYTHON -PathType Leaf)) {
            $sourceEnvironmentPython = Join-Path $root 'agent.venv\Scripts\python.exe'
            if (-not (Test-Path -LiteralPath $sourceEnvironmentPython -PathType Leaf)) {
                throw 'Python 3.12 is required to verify the extracted release.'
            }
            $basePython = (& $sourceEnvironmentPython -c "import pathlib, sys; print(pathlib.Path(sys.base_prefix) / 'python.exe')").Trim()
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $basePython -PathType Leaf)) {
                throw 'Python 3.12 is required to verify the extracted release.'
            }
            $env:FORGEPILOT_PYTHON = $basePython
        }
        Push-Location $extracted
        try {
            & '.\scripts\Setup-Agent.cmd'
            if ($LASTEXITCODE -ne 0) { throw 'Extracted release agent setup failed.' }
            & '.\scripts\Check-Release.cmd'
            if ($LASTEXITCODE -ne 0) { throw 'Extracted release preflight failed.' }
        } finally { Pop-Location }
    } finally {
        if ($null -eq $previousPython) { Remove-Item Env:FORGEPILOT_PYTHON -ErrorAction SilentlyContinue }
        else { $env:FORGEPILOT_PYTHON = $previousPython }
        if (Test-Path -LiteralPath $verifyRoot) { Remove-Item -LiteralPath $verifyRoot -Recurse -Force }
    }
}

$pendingZip = Join-Path $output ('.forgepilot-' + [Guid]::NewGuid().ToString('N') + '.zip')
$pendingChecksum = $pendingZip + '.sha256'
$backupZip = $pendingZip + '.previous'
try {
    try { New-ReleaseArchive $pendingZip } finally {
        Remove-Item -LiteralPath $stage -Recurse -Force
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
