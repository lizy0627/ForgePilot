param([ValidateSet('Start', 'Stop', 'Configure', 'Check')][string]$Action = 'Start')
$ErrorActionPreference = 'Stop'
$stage = '检查本机环境'
$projectRoot = Split-Path $PSScriptRoot -Parent
$pythonExe = if ($env:FORGEPILOT_PYTHON) { $env:FORGEPILOT_PYTHON } else { Join-Path $projectRoot 'agent.venv\Scripts\python.exe' }
$launcherScript = Join-Path $PSScriptRoot 'desktop_launcher.py'
$sha = [Security.Cryptography.SHA256]::Create()
try { $identity = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())))).Replace('-', '').Substring(0, 16) }
finally { $sha.Dispose() }
$settingsDirectory = Join-Path $env:LOCALAPPDATA "ForgePilot\launcher\$identity"
$configFile = Join-Path $settingsDirectory 'config.json'
$keyFile = Join-Path $settingsDirectory 'api-key.dpapi'

function Ask-Default([string]$Label, [string]$Default) {
    $answer = Read-Host "$Label [$Default]"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $Default }
    return $answer.Trim()
}

function Find-OptionalTool([string]$Variable, [string]$Name) {
    $configured = [Environment]::GetEnvironmentVariable($Variable, 'Process')
    if ($configured -and (Test-Path -LiteralPath $configured -PathType Leaf)) { return $configured }
    $tool = Get-Command $Name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tool) { return $tool.Source }
    return $null # Ordinary Q&A does not need development tools.
}

function Test-ModelKey([Security.SecureString]$Secret, [string]$ValidationConfig = $configFile) {
    Write-Host '正在发送一条简短模型请求检查连接（最多等待 30 秒）……'
    $previousKey = $env:FORGEPILOT_API_KEY
    $previousConsoleEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $env:FORGEPILOT_API_KEY = [Net.NetworkCredential]::new('', $Secret).Password
        & $pythonExe -X utf8 -I $launcherScript check --config $ValidationConfig
        if ($LASTEXITCODE -ne 0) { throw '模型连接检查失败。' }
    } finally {
        if ($null -eq $previousKey) { Remove-Item Env:FORGEPILOT_API_KEY -ErrorAction SilentlyContinue }
        else { $env:FORGEPILOT_API_KEY = $previousKey }
        [Console]::OutputEncoding = $previousConsoleEncoding
    }
}

function Save-Key([string]$ValidationConfig = $configFile) {
    Write-Host 'API Key 仅在本机使用 Windows 当前账户加密保存，不写入项目、日志或命令参数。'
    if ($env:FORGEPILOT_API_KEY) { $secret = ConvertTo-SecureString $env:FORGEPILOT_API_KEY -AsPlainText -Force }
    else { $secret = Read-Host '请输入 API Key（输入不显示）' -AsSecureString }
    $pendingKey = Join-Path $settingsDirectory ('api-key-' + [Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        if ($secret.Length -eq 0) { throw 'API Key 不能为空。' }
        Test-ModelKey $secret $ValidationConfig
        $secret | ConvertFrom-SecureString | Set-Content -LiteralPath $pendingKey -Encoding ASCII
        Move-Item -LiteralPath $pendingKey -Destination $keyFile -Force
    } catch {
        Write-Host '新密钥未保存，原有密钥保留。请处理上面的错误后重新配置。' -ForegroundColor Yellow
        throw
    } finally {
        if (Test-Path -LiteralPath $pendingKey) { Remove-Item -LiteralPath $pendingKey }
        if ($secret) { $secret.Dispose() }
    }
}

function Config-Property($Object, [string]$Name, $Default) {
    if ($Object -and $Object.PSObject.Properties.Name -contains $Name) {
        $value = $Object.$Name
        if ($value -is [string] -and -not [string]::IsNullOrWhiteSpace($value)) { return $value.Trim() }
    }
    return $Default
}

try {
    $legacyEnvironment = Join-Path $projectRoot 'agent\.venv'
    if (Test-Path -LiteralPath $legacyEnvironment) {
        Write-Host '警告：agent/.venv 不是当前支持的环境；请使用根目录 agent.venv。旧环境未被删除。' -ForegroundColor Yellow
    }
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        Write-Host '缺少根目录 agent.venv，请先运行 scripts\Setup-Agent.cmd。' -ForegroundColor Red
        exit 2
    }
    if ($Action -eq 'Stop') {
        $stage = '关闭本项目管理的客户端和后端'
        Write-Host '正在关闭 ForgePilot 并保存历史，请稍候……'
        & $pythonExe -X utf8 -I $launcherScript stop --config $configFile
        if ($LASTEXITCODE -ne 0) { throw '关闭失败，请查看上面的提示。' }
        Write-Host 'ForgePilot 已关闭；任务历史、日志与仓库文件保留。'
        exit 0
    }
    if ($Action -eq 'Check') {
        $stage = '读取本机配置和加密密钥'
        if (-not (Test-Path -LiteralPath $configFile) -or -not (Test-Path -LiteralPath $keyFile)) {
            Write-Host '尚未保存模型配置或密钥，请先运行 Configure-ForgePilot.cmd。'
            exit 1
        }
        $secret = (Get-Content -LiteralPath $keyFile -Raw).Trim() | ConvertTo-SecureString
        try {
            $stage = '检查真实模型连接'
            Test-ModelKey $secret
        } finally { $secret.Dispose() }
        Write-Host '检查完成。若刚更新了配置，请先 Stop 再 Start，使运行中的后端加载新配置。'
        exit 0
    }
    if ($Action -eq 'Start') {
        & $pythonExe -X utf8 -I $launcherScript status --config $configFile
        if ($LASTEXITCODE -eq 0) { Write-Host 'ForgePilot 已在运行。如已关掉客户端，请先关闭项目再重新启动。'; exit 0 }
        if ($LASTEXITCODE -ne 3) { throw '无法检查本项目的运行状态。' }
    }
    New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
    $stage = '读取或创建本机配置'
    $hasConfig = Test-Path -LiteralPath $configFile
    $existingConfig = if ($hasConfig) { Get-Content -LiteralPath $configFile -Raw | ConvertFrom-Json } else { $null }
    $pendingConfig = $null
    if (-not $hasConfig -or $Action -eq 'Configure') {
        Write-Host '模型配置：普通聊天、代码任务和对话摘要可以使用不同模型。'
        $storedFallback = Config-Property $existingConfig 'model' 'doubao-seed-2-0-lite-260215'
        $baseUrl = if ($env:FORGEPILOT_BASE_URL) { $env:FORGEPILOT_BASE_URL } else { Config-Property $existingConfig 'base_url' 'https://ark.cn-beijing.volces.com/api/v3' }
        $fallback = if ($env:FORGEPILOT_MODEL) { $env:FORGEPILOT_MODEL } else { $storedFallback }
        $chatDefault = if ($env:FORGEPILOT_CHAT_MODEL) { $env:FORGEPILOT_CHAT_MODEL } else { Config-Property $existingConfig 'chat_model' $fallback }
        $taskDefault = if ($env:FORGEPILOT_TASK_MODEL) { $env:FORGEPILOT_TASK_MODEL } else { Config-Property $existingConfig 'task_model' $fallback }
        $summaryDefault = if ($env:FORGEPILOT_SUMMARY_MODEL) { $env:FORGEPILOT_SUMMARY_MODEL } else { Config-Property $existingConfig 'summary_model' $chatDefault }
        if ($hasConfig) {
            $serverArguments = @($existingConfig.server_args)
            $port = [int]$existingConfig.port
        } else {
            $port = 8765
            $serverArguments = @('--database', (Join-Path $projectRoot 'logs\demo-live.sqlite3'), '--timeout', '120')
            foreach ($toolSpec in @(@('--rg', 'FORGEPILOT_RG', 'rg.exe'), @('--clangd', 'FORGEPILOT_CLANGD', 'clangd.exe'), @('--cmake', 'FORGEPILOT_CMAKE', 'cmake.exe'))) {
                $toolPath = Find-OptionalTool $toolSpec[1] $toolSpec[2]
                if ($toolPath) { $serverArguments += @($toolSpec[0], $toolPath) }
            }
        }
        $config = [ordered]@{
            base_url = (Ask-Default '模型 Base URL' $baseUrl)
            model = (Ask-Default '缺省模型（兼容 FORGEPILOT_MODEL）' $fallback)
            chat_model = (Ask-Default '普通聊天模型' $chatDefault)
            task_model = (Ask-Default '代码任务模型' $taskDefault)
            summary_model = (Ask-Default '对话摘要模型' $summaryDefault)
            port = $port
            server_args = $serverArguments
        }
        $sameModels = ($hasConfig -and $config.base_url -eq $existingConfig.base_url -and
            $config.model -eq $storedFallback -and $config.chat_model -eq (Config-Property $existingConfig 'chat_model' $storedFallback) -and
            $config.task_model -eq (Config-Property $existingConfig 'task_model' $storedFallback) -and
            $config.summary_model -eq (Config-Property $existingConfig 'summary_model' (Config-Property $existingConfig 'chat_model' $storedFallback)))
        if (-not $sameModels) {
            $pendingConfig = Join-Path $settingsDirectory ('config-' + [Guid]::NewGuid().ToString('N') + '.tmp')
            $config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $pendingConfig -Encoding UTF8
        }
    }
    Write-Host "本机配置：$configFile"
    $stage = '验证并保存本机加密密钥'
    $validationConfig = if ($pendingConfig) { $pendingConfig } else { $configFile }
    if ($Action -eq 'Configure' -or -not (Test-Path -LiteralPath $keyFile)) {
        Save-Key $validationConfig
        if ($pendingConfig) { Move-Item -LiteralPath $pendingConfig -Destination $configFile -Force; $pendingConfig = $null }
    } elseif ($pendingConfig) {
        $savedSecret = (Get-Content -LiteralPath $keyFile -Raw).Trim() | ConvertTo-SecureString
        try { Test-ModelKey $savedSecret $validationConfig }
        finally { $savedSecret.Dispose() }
        Move-Item -LiteralPath $pendingConfig -Destination $configFile -Force
        $pendingConfig = $null
    }
    if ($Action -eq 'Configure') { Write-Host '模型连接验证通过，密钥已保存。请依次运行 Stop-ForgePilot.cmd 和 Start-ForgePilot.cmd 使新密钥生效。'; exit 0 }
    $clientExe = Join-Path $projectRoot 'client\build\Release\ForgePilotClient.exe'
    $stage = '检查已构建客户端'
    if (-not (Test-Path -LiteralPath $clientExe)) {
        Write-Host '缺少已构建客户端，请在开发工具可用时运行 scripts\Build-Client.cmd。' -ForegroundColor Red
        exit 2
    }
    $stage = '解密本机密钥（失败时请运行 Configure-ForgePilot.cmd 重新输入）'
    $secret = (Get-Content -LiteralPath $keyFile -Raw).Trim() | ConvertTo-SecureString
    $previousKey = $env:FORGEPILOT_API_KEY
    try {
        $stage = '启动后端与客户端'
        Write-Host '正在启动后端并等待客户端窗口，请稍候……'
        $env:FORGEPILOT_API_KEY = [Net.NetworkCredential]::new('', $secret).Password
        & $pythonExe -X utf8 -I $launcherScript start --config $configFile
        if ($LASTEXITCODE -ne 0) { throw '启动失败，请查看上面的错误与日志路径。' }
    } finally {
        if ($null -eq $previousKey) { Remove-Item Env:FORGEPILOT_API_KEY -ErrorAction SilentlyContinue }
        else { $env:FORGEPILOT_API_KEY = $previousKey }
        $secret.Dispose()
    }
    Write-Host 'ForgePilot 已启动。双击 Stop-ForgePilot.cmd 可关闭客户端和后端。'
} catch {
    if ($pendingConfig -and (Test-Path -LiteralPath $pendingConfig)) { Remove-Item -LiteralPath $pendingConfig }
    # Do not echo arbitrary exception text: credential conversion errors may contain input.
    Write-Host "操作未完成，步骤：$stage。请检查上面的提示、本机 config.json 和 logs\desktop。" -ForegroundColor Red
    exit 1
}
