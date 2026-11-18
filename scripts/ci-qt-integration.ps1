param([string]$Only)

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $root 'agent.venv\Scripts\python.exe'
$runner = Join-Path $root 'client\tests\run_backend_integration.py'
$binary = Join-Path $root 'client\build\Release\ForgePilotBackendTests.exe'
foreach ($path in @($python, $runner, $binary)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing Qt integration input: $path" }
}
if ($env:CI -and (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'local-env.cmd'))) {
    throw 'CI must not read personal local-env.cmd.'
}
foreach ($name in @(
    'FORGEPILOT_API_KEY', 'FORGEPILOT_BASE_URL', 'FORGEPILOT_MODEL_CONFIG',
    'FORGEPILOT_MCP_CONFIG', 'FORGEPILOT_QDRANT_URL',
    'FORGEPILOT_EMBEDDING_API_KEY', 'FORGEPILOT_EMBEDDING_BASE_URL',
    'FORGEPILOT_LOCAL_API_TOKEN',
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AZURE_OPENAI_API_KEY',
    'GOOGLE_API_KEY', 'QDRANT_API_KEY', 'TAVILY_API_KEY'
)) {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}

$output = Join-Path $root 'artifacts\qt-integration'
New-Item -ItemType Directory -Path $output -Force | Out-Null
$env:PYTHONIOENCODING = 'utf-8'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = '127.0.0.1,localhost,::1'
$cases = @(
    @{ name = 'baseline plus mission'; args = @() },
    @{ name = 'feedback'; args = @('--scenario', 'feedback') },
    @{ name = 'joint'; args = @('--scenario', 'joint') },
    @{ name = 'conversations'; args = @('--conversations') },
    @{ name = 'deletion'; args = @('--deletion') },
    @{ name = 'memory'; args = @('--memory') },
    @{ name = 'mcp management'; args = @('--mcp') },
    @{ name = 'mcp runtime'; args = @('--mcp-runtime') }
)
if ($Only -and $Only -notin @($cases | ForEach-Object { $_.name })) {
    throw "Unknown Qt integration case: $Only"
}
foreach ($case in $cases) {
    if ($Only -and $case.name -ne $Only) { continue }
    Write-Output "Qt offline integration: $($case.name)"
    $arguments = @($runner) + $case.args + @('--output-root', $output)
    $log = Join-Path $output (($case.name -replace ' ', '-') + '.log')
    # Native tools may print harmless Git line-ending warnings to stderr.
    # Decide success from the process exit code, not PowerShell's stderr record.
    $previousErrorAction = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $python @arguments *> $log }
    finally { $ErrorActionPreference = $previousErrorAction }
    $exitCode = $LASTEXITCODE
    Get-Content -LiteralPath $log -Tail 12
    if ($exitCode -ne 0) {
        Get-Content -LiteralPath $log -Tail 100
        throw "Qt offline integration failed: $($case.name)"
    }
}
