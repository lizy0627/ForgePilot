param(
    [switch]$Fast,
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
if ($Fast.IsPresent -eq $Full.IsPresent) { throw 'Select exactly one of -Fast or -Full.' }

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$python = Join-Path $root 'agent.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run locked agent setup first.' }
# Tests inject fake providers or local loopback servers. Remove inherited credentials
# and endpoint configuration before any subprocess is created.
$sensitive = @(
    'FORGEPILOT_API_KEY', 'FORGEPILOT_BASE_URL', 'FORGEPILOT_MODEL_CONFIG',
    'FORGEPILOT_MCP_CONFIG', 'FORGEPILOT_QDRANT_URL',
    'FORGEPILOT_EMBEDDING_API_KEY', 'FORGEPILOT_EMBEDDING_BASE_URL',
    'FORGEPILOT_LOCAL_API_TOKEN',
    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AZURE_OPENAI_API_KEY',
    'GOOGLE_API_KEY', 'QDRANT_API_KEY', 'TAVILY_API_KEY'
)
foreach ($name in $sensitive) { Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue }
$env:PYTHONIOENCODING = 'utf-8'
$env:HTTP_PROXY = 'http://127.0.0.1:9'
$env:HTTPS_PROXY = 'http://127.0.0.1:9'
$env:ALL_PROXY = 'http://127.0.0.1:9'
$env:NO_PROXY = '127.0.0.1,localhost,::1'

$fastTests = @(
    'tests/test_graph_orchestration.py',
    'tests/test_graph_fanout.py',
    'tests/test_artifact_protocol.py',
    'tests/test_sqlite_artifact_store.py',
    'tests/test_security.py',
    'tests/test_policy.py',
    'tests/test_scope_policy.py',
    'tests/test_file_access_security.py',
    'tests/test_mission_api_workflow.py',
    'tests/test_mission_events.py',
    'tests/test_mission_sse_network.py',
    'tests/test_mission_service.py',
    'tests/test_mission_legacy_delivery.py',
    'tests/test_delivery_baseline_approval.py',
    'tests/test_model_gateway_boundaries.py',
    'tests/test_implementation_boundaries.py',
    'tests/test_workspace_manager.py',
    'tests/test_workspace_delivery.py',
    'tests/test_recovery_workspace.py',
    'tests/test_build_process.py',
    'tests/test_rag_retrieval.py',
    'tests/test_rag_lifecycle.py',
    'tests/test_recovery.py',
    'tests/test_recovery_boundaries.py',
    'tests/test_protocol_v2.py'
)
$artifacts = Join-Path $root 'artifacts'
New-Item -ItemType Directory -Path $artifacts -Force | Out-Null
$dataFile = Join-Path $artifacts '.coverage-ci'
$jsonFile = Join-Path $artifacts 'python-coverage.json'

Push-Location (Join-Path $root 'agent')
try {
    if ($Fast) {
        foreach ($test in $fastTests) {
            if (-not (Test-Path -LiteralPath $test -PathType Leaf)) { throw "Missing PR test file: $test" }
        }
        $selection = $fastTests
    } else {
        $selection = @('tests')
    }
    & $python -m coverage run --branch --source=forgepilot --data-file=$dataFile -m pytest -q @selection
    if ($LASTEXITCODE -ne 0) { throw 'Python tests failed.' }
    & $python -m coverage json --data-file=$dataFile -o $jsonFile
    if ($LASTEXITCODE -ne 0) { throw 'Coverage JSON export failed.' }
    if ($Full) {
        & $python (Join-Path $PSScriptRoot 'check_core_coverage.py') $jsonFile
        if ($LASTEXITCODE -ne 0) { throw 'Core coverage gate failed.' }
    }
} finally {
    Pop-Location
}
