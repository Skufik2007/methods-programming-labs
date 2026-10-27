<#
.SYNOPSIS
  Локальный запуск стенда без Docker (Windows PowerShell 5.1): нативные NATS, Jaeger, Garnet (Redis),
  Go-агенты, LLM-агенты, дашборд и автомасштабировщик (exec-бэкенд).

.DESCRIPTION
  start   собирает Go-бинарники и запускает все процессы в фоне; логи в .run\logs\, PID в .run\pids.json
  stop    останавливает только процессы, записанные в .run\pids.json (вместе с их дочерними)
  status  показывает, какие из своих процессов живы, и открыты ли порты

  Переменные окружения те же, что в compose.yaml (REMINDER_DEMO_DELAY, SURVEY_DEMO_DELAY,
  AGENT_SLOW_MS у триажа, LLM_MODE и т. д.); задаются параметрами ниже.

.EXAMPLE
  .\scripts\dev.ps1 start
  .\scripts\dev.ps1 start -TriageSlowMs 800 -LlmMode llm
  .\scripts\dev.ps1 status
  .\scripts\dev.ps1 stop
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0, Mandatory = $true)]
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action,

    # Каталог с нативными инструментами и пути к ним (можно переопределить по отдельности).
    [string]$ToolsDir = 'D:\dev\lab13-tools',
    [string]$NatsExe = '',
    [string]$JaegerExe = '',
    [string]$GarnetExe = '',
    [string]$GoExe = 'go',

    # Python из виртуального окружения репозитория.
    [string]$PythonExe = '',

    # Параметры демонстрации (как в compose.yaml).
    [string]$ReminderDemoDelay = '5s',
    [string]$SurveyDemoDelay = '10s',
    [int]$TriageSlowMs = 0,
    [double]$AgentFailRate = 0,
    [ValidateSet('fallback', 'llm')]
    [string]$LlmMode = 'fallback',
    [string]$OllamaUrl = 'http://127.0.0.1:11434',
    [string]$LogLevel = 'info'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $Root '.run'
$LogDir = Join-Path $RunDir 'logs'
$PidFile = Join-Path $RunDir 'pids.json'
$AgentsDir = Join-Path $Root 'agents'
$BinDir = Join-Path $AgentsDir 'bin'

if (-not $NatsExe) { $NatsExe = Join-Path $ToolsDir 'bin\nats-server.exe' }
if (-not $JaegerExe) { $JaegerExe = Join-Path $ToolsDir 'bin\jaeger.exe' }
if (-not $GarnetExe) { $GarnetExe = Join-Path $ToolsDir 'win-x64-based-readytorun\net8.0\GarnetServer.exe' }
if (-not $PythonExe) { $PythonExe = Join-Path $Root '.venv\Scripts\python.exe' }

$NatsUrl = 'nats://localhost:4222'
$RedisUrl = 'redis://localhost:6379/0'
$OtlpUrl = 'http://localhost:4318'

# Порты, которые занимает стенд: по ним start понимает, что стенд (чужой или прежний) уже запущен.
$Ports = [ordered]@{ nats = 4222; redis = 6379; jaeger = 16686; dashboard = 8000; autoscaler = 8090 }

function Test-Port([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($async)
        return $true
    }
    catch { return $false }
    finally { $client.Close() }
}

function Wait-Port([string]$Name, [int]$Port, [int]$TimeoutSec = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { return }
        Start-Sleep -Milliseconds 300
    }
    throw "$Name не открыл порт $Port за $TimeoutSec с (см. $LogDir)"
}

function Read-Pids {
    if (-not (Test-Path $PidFile)) { return @() }
    $raw = Get-Content -Raw -Encoding UTF8 $PidFile
    if (-not $raw.Trim()) { return @() }
    return @($raw | ConvertFrom-Json)
}

# Процесс считается «своим и живым», только если совпали PID и время запуска (защита от повторного
# использования PID после перезагрузки или долгой паузы).
function Get-OwnProcess($Entry) {
    $proc = Get-Process -Id $Entry.pid -ErrorAction SilentlyContinue
    if (-not $proc) { return $null }
    try {
        if ($proc.StartTime.ToUniversalTime().ToString('o') -ne $Entry.started) { return $null }
    }
    catch { return $null }
    return $proc
}

$script:Started = New-Object System.Collections.ArrayList

# Запуск фонового процесса: переменные окружения задаются только на время запуска,
# stdout и stderr пишутся в .run\logs\<name>.log и <name>.err.log.
function Start-Svc([string]$Name, [string]$Exe, [string[]]$ArgList, [string]$WorkDir, [hashtable]$EnvVars) {
    if (-not (Test-Path $Exe) -and -not (Get-Command $Exe -ErrorAction SilentlyContinue)) {
        throw "Не найден исполняемый файл для ${Name}: $Exe"
    }
    $saved = @{}
    foreach ($key in $EnvVars.Keys) {
        $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process')
        [Environment]::SetEnvironmentVariable($key, [string]$EnvVars[$key], 'Process')
    }
    try {
        $params = @{
            FilePath               = $Exe
            WorkingDirectory       = $WorkDir
            WindowStyle            = 'Hidden'
            PassThru               = $true
            RedirectStandardOutput = (Join-Path $LogDir "$Name.log")
            RedirectStandardError  = (Join-Path $LogDir "$Name.err.log")
        }
        # Пустой ArgumentList Start-Process не принимает. Путь репозитория содержит пробелы, а
        # Start-Process в PS 5.1 сам аргументы не экранирует, поэтому каждый берётся в кавычки.
        if ($ArgList -and $ArgList.Count -gt 0) {
            $params['ArgumentList'] = ($ArgList | ForEach-Object { '"' + $_ + '"' }) -join ' '
        }
        $proc = Start-Process @params
    }
    finally {
        foreach ($key in $saved.Keys) {
            [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process')
        }
    }
    $entry = [pscustomobject]@{
        name    = $Name
        pid     = $proc.Id
        started = $proc.StartTime.ToUniversalTime().ToString('o')
    }
    [void]$script:Started.Add($entry)
    # PID сохраняется сразу после каждого запуска: при сбое на полпути stop знает, что гасить.
    ConvertTo-Json -Depth 3 -InputObject @($script:Started) | Set-Content -Encoding UTF8 $PidFile
    Write-Host ("  {0,-20} pid {1}" -f $Name, $proc.Id)
}

function Stop-Tree($Entry) {
    $proc = Get-OwnProcess $Entry
    if (-not $proc) {
        Write-Host ("  {0,-20} уже не запущен" -f $Entry.name)
        return
    }
    # /T — вместе с дочерними (экземпляры триажа, запущенные автомасштабировщиком).
    & taskkill.exe /PID $Entry.pid /T /F | Out-Null
    Write-Host ("  {0,-20} остановлен (pid {1})" -f $Entry.name, $Entry.pid)
}

function Invoke-Start {
    $existing = @(Read-Pids | Where-Object { Get-OwnProcess $_ })
    if ($existing.Count -gt 0) {
        throw "Стенд уже запущен (процессов: $($existing.Count)). Сначала: scripts\dev.ps1 stop"
    }
    $busy = @($Ports.Keys | Where-Object { Test-Port $Ports[$_] } | ForEach-Object { "$_ ($($Ports[$_]))" })
    if ($busy.Count -gt 0) {
        throw "Порты заняты чужим стендом или процессами: $($busy -join ', '). Остановите их и повторите."
    }
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    New-Item -ItemType Directory -Force -Path (Join-Path $RunDir 'js') | Out-Null
    Set-Content -Encoding UTF8 -Path $PidFile -Value '[]'

    Write-Host 'Сборка Go-бинарников...'
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    Push-Location $AgentsDir
    try {
        & $GoExe build -o (Join-Path $BinDir 'agent.exe') ./cmd/agent
        if ($LASTEXITCODE -ne 0) { throw 'go build ./cmd/agent завершился с ошибкой' }
        & $GoExe build -o (Join-Path $BinDir 'autoscaler.exe') ./cmd/autoscaler
        if ($LASTEXITCODE -ne 0) { throw 'go build ./cmd/autoscaler завершился с ошибкой' }
    }
    finally { Pop-Location }

    $common = @{
        NATS_URL                    = $NatsUrl
        OTEL_EXPORTER_OTLP_ENDPOINT = $OtlpUrl
        LOG_LEVEL                   = $LogLevel
    }
    $goEnv = $common + @{ REDIS_URL = $RedisUrl; AGENT_FAIL_RATE = [string]$AgentFailRate }
    $agentExe = Join-Path $BinDir 'agent.exe'

    Write-Host 'Инфраструктура...'
    Start-Svc 'nats' $NatsExe @('-js', '-sd', (Join-Path $RunDir 'js'), '-m', '8222') $Root @{}
    Wait-Port 'nats' 4222
    Start-Svc 'garnet' $GarnetExe @('--port', '6379') $Root @{}
    Wait-Port 'garnet' 6379
    Start-Svc 'jaeger' $JaegerExe @() $Root @{}
    Wait-Port 'jaeger' 4318 60

    Write-Host 'Go-агенты...'
    $roles = Join-Path $Root 'roles'
    $agents = @(
        @{ Name = 'triage'; Role = 'triage.md'; Env = @{ AGENT_ID = 'triage-1'; AGENT_SLOW_MS = [string]$TriageSlowMs } },
        @{ Name = 'booking-a'; Role = 'booking-a.md'; Env = @{ AGENT_ID = 'booking-a' } },
        @{ Name = 'booking-b'; Role = 'booking-b.md'; Env = @{ AGENT_ID = 'booking-b' } },
        @{ Name = 'reminder'; Role = 'reminder.md'; Env = @{ AGENT_ID = 'reminder-1'; REMINDER_DEMO_DELAY = $ReminderDemoDelay } },
        @{ Name = 'feedback'; Role = 'feedback.md'; Env = @{ AGENT_ID = 'feedback-1'; SURVEY_DEMO_DELAY = $SurveyDemoDelay } }
    )
    foreach ($a in $agents) {
        Start-Svc $a.Name $agentExe @('-role', (Join-Path $roles $a.Role)) $Root ($goEnv + $a.Env)
    }

    Write-Host 'LLM-агенты...'
    $llmEnv = $common + @{ LLM_MODE = $LlmMode; OLLAMA_URL = $OllamaUrl; PYTHONUNBUFFERED = '1'; PYTHONUTF8 = '1' }
    Start-Svc 'llm-intake' $PythonExe @('-m', 'mas.llm_agent', '--role', (Join-Path $roles 'intake.md')) $Root `
        ($llmEnv + @{ AGENT_ID = 'llm-intake-1' })
    Start-Svc 'llm-feedback-reply' $PythonExe @('-m', 'mas.llm_agent', '--role', (Join-Path $roles 'feedback_reply.md')) $Root `
        ($llmEnv + @{ AGENT_ID = 'llm-feedback-reply-1' })

    Write-Host 'Автомасштабировщик и дашборд...'
    # Автомасштабировщик запускается из agents\: пути в deploy\autoscaler.local.yaml (./bin/agent.exe,
    # ../roles/triage.md) заданы относительно этого каталога. Добавочные экземпляры наследуют его окружение.
    Start-Svc 'autoscaler' (Join-Path $BinDir 'autoscaler.exe') @('-config', (Join-Path $Root 'deploy\autoscaler.local.yaml')) `
        $AgentsDir ($goEnv + @{ AGENT_SLOW_MS = [string]$TriageSlowMs })
    $dashEnv = $common + @{
        DASHBOARD_HOST   = '127.0.0.1'
        DASHBOARD_PORT   = '8000'
        AUTOSCALER_URL   = 'http://localhost:8090'
        JAEGER_UI_URL    = 'http://localhost:16686'
        PYTHONUNBUFFERED = '1'
        PYTHONUTF8       = '1'
    }
    Start-Svc 'dashboard' $PythonExe @('-m', 'mas.dashboard') $Root $dashEnv
    Wait-Port 'dashboard' 8000 60

    Write-Host ''
    Write-Host 'Готово. Дашборд: http://localhost:8000  Jaeger: http://localhost:16686  Логи: .run\logs\'
}

function Invoke-Stop {
    $entries = @(Read-Pids)
    if ($entries.Count -eq 0) {
        Write-Host 'Нет записанных процессов (.run\pids.json пуст или отсутствует).'
        return
    }
    # В обратном порядке запуска: сначала дашборд и агенты, потом инфраструктура.
    [array]::Reverse($entries)
    foreach ($entry in $entries) { Stop-Tree $entry }
    Set-Content -Encoding UTF8 -Path $PidFile -Value '[]'
}

function Invoke-Status {
    $entries = @(Read-Pids)
    if ($entries.Count -eq 0) { Write-Host 'Процессы, запущенные этим скриптом: нет.' }
    foreach ($entry in $entries) {
        $state = if (Get-OwnProcess $entry) { 'работает' } else { 'не запущен' }
        Write-Host ("  {0,-20} pid {1,-7} {2}" -f $entry.name, $entry.pid, $state)
    }
    Write-Host 'Порты:'
    foreach ($name in $Ports.Keys) {
        $state = if (Test-Port $Ports[$name]) { 'открыт' } else { 'закрыт' }
        Write-Host ("  {0,-20} {1,-6} {2}" -f $name, $Ports[$name], $state)
    }
}

try {
    switch ($Action) {
        'start' { Invoke-Start }
        'stop' { Invoke-Stop }
        'status' { Invoke-Status }
    }
}
catch {
    Write-Host "Ошибка: $($_.Exception.Message)" -ForegroundColor Red
    if ($Action -eq 'start') { Write-Host 'Уже запущенные части можно остановить: scripts\dev.ps1 stop' }
    exit 1
}
