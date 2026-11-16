param([ValidateSet('Start', 'Stop', 'Configure', 'Check')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
$packagedRuntimeFile = Join-Path $projectRoot 'bin\ForgePilotAgent.exe'
$packagedRuntimeAvailable = Test-Path -LiteralPath $packagedRuntimeFile -PathType Leaf
$pythonExe = if ($packagedRuntimeAvailable) { $packagedRuntimeFile } elseif ($env:FORGEPILOT_PYTHON) { $env:FORGEPILOT_PYTHON } else { Join-Path $projectRoot 'agent.venv\Scripts\python.exe' }
$launcherPrefix = if ($packagedRuntimeAvailable) { @() } else { @('-X', 'utf8', '-I', (Join-Path $PSScriptRoot 'desktop_launcher.py')) }
$sha = [Security.Cryptography.SHA256]::Create()
try { $identity = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 16) }
finally { $sha.Dispose() }
$configFile = Join-Path $env:LOCALAPPDATA "ForgePilot\launcher\$identity\config.json"
try {
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        Write-Host '缺少冻结后端或根目录 agent.venv，请重新解压发布包或运行 scripts\Setup-Agent.cmd。'
        exit 2
    }
    # GUI and command entrypoints share Python configuration, DPAPI and validation.
    $operation = switch ($Action) {
        'Start' { 'desktop' }
        'Configure' { 'configure' }
        'Stop' { 'stop' }
        'Check' { 'check-saved' }
    }
    & $pythonExe @launcherArgs $operation --config $configFile
    exit $LASTEXITCODE
} catch {
    # No raw exception/configuration/credential values go to console output.
    Write-Host '操作未完成，请检查 ForgePilot 安装及本机配置目录权限。'
    exit 1
}
