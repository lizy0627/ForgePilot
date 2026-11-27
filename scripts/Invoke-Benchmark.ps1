param(
    [switch]$CheckOnly,
    [string]$ReviewModel,
    [string]$TaskModelFamily,
    [string]$ReviewModelFamily,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$BenchmarkArgs
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
if (-not $CheckOnly) {
    $index = if ($BenchmarkArgs) { [Array]::IndexOf($BenchmarkArgs, '--mode') } else { -1 }
    if ($index -lt 0 -or $BenchmarkArgs[$index + 1] -cne 'real') {
        [Console]::Error.WriteLine('Use this wrapper only with an explicit --mode real benchmark command.')
        exit 1
    }
}

# Keep public arguments intact across Windows PowerShell and the toolchain .cmd.
# The Python process reads the encrypted credential directly from the saved configuration.
$previous = $env:FORGEPILOT_BENCHMARK_INVOCATION
try {
    $env:FORGEPILOT_BENCHMARK_INVOCATION = ConvertTo-Json -Compress -Depth 3 -InputObject @{
        root = $projectRoot; arguments = $BenchmarkArgs
        review_model = $ReviewModel; task_model_family = $TaskModelFamily; review_model_family = $ReviewModelFamily
    }
    if ($CheckOnly) {
        $frozen = Join-Path $projectRoot 'bin\ForgePilotAgent.exe'
        if (Test-Path -LiteralPath $frozen -PathType Leaf) {
            & $frozen benchmark-check
        } else {
            $python = if ($env:FORGEPILOT_PYTHON) { $env:FORGEPILOT_PYTHON } else { Join-Path $projectRoot 'agent.venv\Scripts\python.exe' }
            & $python -X utf8 -I (Join-Path $PSScriptRoot 'desktop_launcher.py') benchmark-check
        }
    } else {
        & (Join-Path $PSScriptRoot 'Run-Benchmark.cmd') --saved-launcher-settings
    }
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('Benchmark launcher failed. Check the ForgePilot installation and saved configuration.')
    exit 1
} finally {
    $env:FORGEPILOT_BENCHMARK_INVOCATION = $previous
}
