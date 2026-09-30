# Process-local training selection; no PATH or Agent configuration changes.
$trainingEnvironment = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\training\.venv'))
$trainingPython = Join-Path $trainingEnvironment 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $trainingPython -PathType Leaf)) {
    throw 'Training environment missing. Run scripts\Setup-Training.cmd first.'
}
$env:UV_PROJECT_ENVIRONMENT = $trainingEnvironment
$env:UV_PYTHON = $trainingPython
$env:UV_PYTHON_DOWNLOADS = 'never'
$trainingStorage = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\logs\model-training'))
$env:FORGEPILOT_TRAINING_HOME = $trainingStorage
$env:TORCH_HOME = Join-Path $trainingStorage 'cache\torch'
$env:HF_HOME = Join-Path $trainingStorage 'cache\huggingface'
foreach ($trainingSubdirectory in @('data', 'checkpoints', 'exports', 'runs', 'cache\torch', 'cache\huggingface')) {
    New-Item -ItemType Directory -Path (Join-Path $trainingStorage $trainingSubdirectory) -Force | Out-Null
}
