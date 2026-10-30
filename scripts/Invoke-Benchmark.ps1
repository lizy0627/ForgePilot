param(
    [switch]$CheckOnly,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BenchmarkArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$sha = [Security.Cryptography.SHA256]::Create()
try {
    $bytes = [Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())
    $identity = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').Substring(0, 16)
} finally {
    $sha.Dispose()
}
$settingsDirectory = Join-Path $env:LOCALAPPDATA "ForgePilot\launcher\$identity"
$configFile = Join-Path $settingsDirectory 'config.json'
$keyFile = Join-Path $settingsDirectory 'api-key.dpapi'

if (-not (Test-Path -LiteralPath $configFile -PathType Leaf) -or
    -not (Test-Path -LiteralPath $keyFile -PathType Leaf)) {
    throw 'Saved model configuration or encrypted API key is missing. Run Configure-ForgePilot.cmd.'
}

$config = Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json
$baseUrl = $config.base_url
$taskModel = if ($config.task_model) { $config.task_model } else { $config.model }
if ($baseUrl -isnot [string] -or [string]::IsNullOrWhiteSpace($baseUrl) -or
    $taskModel -isnot [string] -or [string]::IsNullOrWhiteSpace($taskModel)) {
    throw 'Saved model endpoint or task model is missing. Run Configure-ForgePilot.cmd.'
}

try {
    $secret = (Get-Content -LiteralPath $keyFile -Raw).Trim() | ConvertTo-SecureString
    if ($secret.Length -eq 0) { throw 'empty-key' }
} catch {
    throw 'Encrypted API key is unavailable to this Windows user. Run Configure-ForgePilot.cmd.'
}

try {
    if ($CheckOnly) {
        Write-Output 'saved_model_config=present'
        Write-Output 'encrypted_api_key=decryptable'
        Write-Output 'api_connectivity=not_checked'
        Write-Output 'benchmark_model_tokens=0'
        exit 0
    }

    if (-not $BenchmarkArgs -or $BenchmarkArgs -notcontains '--mode' -or
        $BenchmarkArgs -notcontains 'real') {
        throw 'Use this wrapper only with an explicit --mode real benchmark command.'
    }
    $names = @('FORGEPILOT_BASE_URL', 'FORGEPILOT_TASK_MODEL', 'FORGEPILOT_API_KEY')
    $previous = @{}
    foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        $env:FORGEPILOT_BASE_URL = $baseUrl
        $env:FORGEPILOT_TASK_MODEL = $taskModel
        $env:FORGEPILOT_API_KEY = [Net.NetworkCredential]::new('', $secret).Password
        & (Join-Path $PSScriptRoot 'Run-Benchmark.cmd') @BenchmarkArgs
        $result = $LASTEXITCODE
    } finally {
        foreach ($name in $names) {
            if ($null -eq $previous[$name]) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
            else { [Environment]::SetEnvironmentVariable($name, $previous[$name], 'Process') }
        }
    }
    exit $result
} finally {
    $secret.Dispose()
}
