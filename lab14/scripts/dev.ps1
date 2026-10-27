<#
.SYNOPSIS
    Локальный стенд ЛР14 без Docker: etcd, NATS, эмулятор, два сборщика, потребитель, дашборд.
.DESCRIPTION
    dev.ps1 start | stop | status
    Логи - .run\logs, PID - .run\pids.json. stop завершает только процессы из pids.json.
    Дашборд: http://127.0.0.1:8000
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'

$Root    = Split-Path -Parent $PSScriptRoot
$RunDir  = Join-Path $Root '.run'
$LogDir  = Join-Path $RunDir 'logs'
$BinDir  = Join-Path $RunDir 'bin'
$PidFile = Join-Path $RunDir 'pids.json'

# Пути к инструментам из задания; при необходимости переопределяются переменными окружения.
$EtcdExe = if ($env:LAB14_ETCD) { $env:LAB14_ETCD } else { 'D:\dev\lab14-tools\etcd\etcd-v3.7.2-windows-amd64\etcd.exe' }
$NatsExe = if ($env:LAB14_NATS) { $env:LAB14_NATS } else { 'D:\dev\lab13-tools\bin\nats-server.exe' }
$PyExe   = Join-Path $Root '.venv\Scripts\python.exe'

# Порты стенда. Для проверки доступности (TCP) и вывода в status.
$Ports = [ordered]@{
    'etcd'        = 2379
    'nats'        = 4222
    'sensorsim'   = 8080
    'collector-1' = 9101
    'collector-2' = 9102
    'dashboard'   = 8000
}

function Read-Pids {
    if (Test-Path $PidFile) {
        $obj = Get-Content $PidFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $res = [ordered]@{}
        foreach ($p in $obj.PSObject.Properties) { $res[$p.Name] = $p.Value }
        return $res
    }
    return [ordered]@{}
}

function Save-Pids($map) {
    if (-not (Test-Path $RunDir)) { New-Item -ItemType Directory -Force $RunDir | Out-Null }
    # Set-Content -Encoding UTF8 в PS 5.1 добавляет BOM; ConvertFrom-Json его переносит
    ($map | ConvertTo-Json) | Set-Content -Path $PidFile -Encoding UTF8
}

# Процесс считаем «своим», только если PID жив и время старта совпадает с записанным
# (защита от повторного использования PID после перезагрузки или долгой паузы).
function Get-OwnProcess($entry) {
    $p = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
    if ($null -eq $p) { return $null }
    try {
        if ($p.StartTime.ToUniversalTime().ToString('o') -ne $entry.started) { return $null }
    } catch { return $null }
    return $p
}

function Test-Port([int]$Port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
        if ($iar.AsyncWaitHandle.WaitOne(500) -and $c.Connected) { return $true }
        return $false
    } catch { return $false } finally { $c.Close() }
}

function Wait-Port([string]$Name, [int]$Port, [int]$TimeoutSec = 30) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-Port $Port) { return }
        Start-Sleep -Milliseconds 300
    }
    throw "$Name не открыл порт $Port за $TimeoutSec с; см. $LogDir"
}

# Запуск процесса с переменными окружения и логами. Start-Process в PS 5.1 наследует окружение
# текущего процесса, поэтому временно выставляем переменные и затем возвращаем прежние значения.
function Start-Component {
    param(
        [string]$Name, [string]$Exe, [string[]]$ArgList = @(), [hashtable]$EnvVars = @{},
        [string]$WorkDir = $Root, [hashtable]$Pids
    )
    $saved = @{}
    foreach ($k in $EnvVars.Keys) {
        $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
        [Environment]::SetEnvironmentVariable($k, [string]$EnvVars[$k], 'Process')
    }
    try {
        $sp = @{
            FilePath               = $Exe
            WorkingDirectory       = $WorkDir
            RedirectStandardOutput = (Join-Path $LogDir "$Name.out.log")
            RedirectStandardError  = (Join-Path $LogDir "$Name.err.log")
            WindowStyle            = 'Hidden'
            PassThru               = $true
        }
        # пустой ArgumentList в PS 5.1 вызывает ошибку - передаём только непустой
        if ($ArgList.Count -gt 0) { $sp['ArgumentList'] = $ArgList }
        $proc = Start-Process @sp
    } finally {
        foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
    }
    $Pids[$Name] = [ordered]@{
        pid     = $proc.Id
        started = $proc.StartTime.ToUniversalTime().ToString('o')
    }
    Save-Pids $Pids
    Write-Host ("  {0,-12} PID {1}" -f $Name, $proc.Id)
}

function Invoke-Start {
    $pids = Read-Pids
    foreach ($n in @($pids.Keys)) {
        if ($null -ne (Get-OwnProcess $pids[$n])) {
            throw "Стенд уже запущен ($n, PID $($pids[$n].pid)). Сначала: dev.ps1 stop"
        }
    }
    foreach ($tool in @($EtcdExe, $NatsExe, $PyExe)) {
        if (-not (Test-Path $tool)) { throw "Не найден: $tool" }
    }
    foreach ($kv in $Ports.GetEnumerator()) {
        if (Test-Port $kv.Value) { throw "Порт $($kv.Value) ($($kv.Key)) уже занят другим процессом" }
    }
    foreach ($d in @($RunDir, $LogDir, $BinDir)) { New-Item -ItemType Directory -Force $d | Out-Null }
    $pids = [ordered]@{}

    Write-Host 'Сборка Go-бинарников...'
    Push-Location (Join-Path $Root 'go')
    try {
        & go build -o (Join-Path $BinDir 'sensorsim.exe') ./cmd/sensorsim
        if ($LASTEXITCODE -ne 0) { throw 'go build sensorsim завершился с ошибкой' }
        & go build -o (Join-Path $BinDir 'collector.exe') ./cmd/collector
        if ($LASTEXITCODE -ne 0) { throw 'go build collector завершился с ошибкой' }
    } finally { Pop-Location }

    Write-Host 'Запуск:'
    try {
        Start-Component -Name 'etcd' -Exe $EtcdExe -Pids $pids -ArgList @(
            '--name', 'dev0', '--data-dir', (Join-Path $RunDir 'etcd-data'),
            '--listen-client-urls', 'http://127.0.0.1:2379',
            '--advertise-client-urls', 'http://127.0.0.1:2379',
            '--listen-peer-urls', 'http://127.0.0.1:2380',
            '--initial-advertise-peer-urls', 'http://127.0.0.1:2380',
            '--initial-cluster', 'dev0=http://127.0.0.1:2380')
        Wait-Port 'etcd' $Ports['etcd']

        Start-Component -Name 'nats' -Exe $NatsExe -Pids $pids -ArgList @(
            '-js', '-a', '127.0.0.1', '-p', '4222', '-sd', (Join-Path $RunDir 'nats-data'), '-m', '8222')
        Wait-Port 'nats' $Ports['nats']

        Start-Component -Name 'sensorsim' -Exe (Join-Path $BinDir 'sensorsim.exe') -Pids $pids -EnvVars @{
            ADDR = '127.0.0.1:8080'; DEVICES = '40'; PERIOD_MS = '500'; SEED = '14' }
        Wait-Port 'sensorsim' $Ports['sensorsim']

        # Общие настройки сборщиков; различаются id, порты и каталоги вывода.
        foreach ($i in 1, 2) {
            $env1 = @{
                COLLECTOR_ID = "collector-$i"; SIM_URL = 'http://127.0.0.1:8080'
                COORD = 'etcd'; ETCD_ENDPOINTS = '127.0.0.1:2379'
                NATS_URL = 'nats://127.0.0.1:4222'; SINKS = 'files,nats,flight'; EMIT = 'both'
                OUT_DIR = (Join-Path $RunDir "data\c$i")
                STATUS_ADDR = "127.0.0.1:910$i"; FLIGHT_ADDR = "127.0.0.1:881$(4 + $i)"
            }
            Start-Component -Name "collector-$i" -Exe (Join-Path $BinDir 'collector.exe') -Pids $pids -EnvVars $env1
        }
        Wait-Port 'collector-1' $Ports['collector-1']
        Wait-Port 'collector-2' $Ports['collector-2']

        $pyEnv = @{ PYTHONPATH = (Join-Path $Root 'python'); PYTHONUTF8 = '1'; NATS_URL = 'nats://127.0.0.1:4222' }
        Start-Component -Name 'streaming' -Exe $PyExe -Pids $pids -EnvVars $pyEnv -ArgList @('-m', 'vitals.streaming')
        $pyEnv['DASHBOARD_HOST'] = '127.0.0.1'; $pyEnv['DASHBOARD_PORT'] = '8000'
        Start-Component -Name 'dashboard' -Exe $PyExe -Pids $pids -EnvVars $pyEnv -ArgList @('-m', 'vitals.dashboard')
        Wait-Port 'dashboard' $Ports['dashboard'] 60
    } catch {
        Write-Host "Ошибка запуска: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host 'Останавливаю уже запущенные компоненты...'
        Invoke-Stop
        throw
    }
    Write-Host 'Стенд запущен. Дашборд: http://127.0.0.1:8000, логи: .run\logs'
}

function Invoke-Stop {
    $pids = Read-Pids
    if ($pids.Count -eq 0) { Write-Host 'Нечего останавливать (нет .run\pids.json).'; return }
    # В обратном порядке запуска: сначала потребители, затем сборщики, затем инфраструктура.
    $names = @($pids.Keys)
    [array]::Reverse($names)
    foreach ($n in $names) {
        $p = Get-OwnProcess $pids[$n]
        if ($null -eq $p) { Write-Host ("  {0,-12} уже не работает" -f $n); continue }
        # venv-python на Windows запускает дочерний процесс - завершаем дерево строго по PID
        & taskkill /PID $p.Id /T /F | Out-Null
        Write-Host ("  {0,-12} остановлен (PID {1})" -f $n, $p.Id)
    }
    Remove-Item $PidFile -ErrorAction SilentlyContinue
}

function Invoke-Status {
    $pids = Read-Pids
    if ($pids.Count -eq 0) { Write-Host 'Стенд не запущен.'; return }
    foreach ($n in $pids.Keys) {
        $alive = $null -ne (Get-OwnProcess $pids[$n])
        $port = if ($Ports.Contains($n)) { $Ports[$n] } else { $null }
        $listen = if ($null -ne $port) { if (Test-Port $port) { "порт $port открыт" } else { "порт $port закрыт" } } else { '-' }
        Write-Host ("{0,-12} PID {1,-7} {2,-8} {3}" -f $n, $pids[$n].pid, $(if ($alive) { 'работает' } else { 'нет' }), $listen)
    }
}

switch ($Action) {
    'start'  { Invoke-Start }
    'stop'   { Invoke-Stop }
    'status' { Invoke-Status }
}
