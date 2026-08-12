$ErrorActionPreference = 'Stop'
$taskName = 'AmazonProcessor-Unattended'
$apiTaskName = 'AmazonProcessor-API'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runtimeRoot = Join-Path $projectRoot '.runtime\server'
$heartbeatPath = Join-Path $runtimeRoot 'heartbeat.json'
$statePath = Join-Path $runtimeRoot 'watchdog_state.json'
$logDirectory = Join-Path $runtimeRoot 'logs'
$logPath = Join-Path $logDirectory (
    'watchdog_' + (Get-Date -Format 'yyyyMMdd') + '.log'
)
$envPath = Join-Path $projectRoot '.env'
$maintenancePath = Join-Path $runtimeRoot 'maintenance.json'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
if (Test-Path -LiteralPath $maintenancePath -PathType Leaf) {
    try {
        $maintenance = Get-Content `
            -LiteralPath $maintenancePath `
            -Raw | ConvertFrom-Json
        if ($maintenance.enabled) { exit 0 }
    }
    catch { exit 0 }
}
$state = @{ worker_stale = 0; api_stale = 0 }
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try {
        $loaded = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $state.worker_stale = [int]$loaded.worker_stale
        $state.api_stale = [int]$loaded.api_stale
    }
    catch { }
}

function Write-WatchdogLog([string]$message) {
    $stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -LiteralPath $logPath -Encoding UTF8 -Value "[$stamp] $message"
}

function Restart-RegisteredTask([string]$name, [string]$reason) {
    Write-WatchdogLog "$name 异常：$reason；执行重启"
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $name
}

function Get-TaskRunning([string]$name) {
    $task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    return $null -ne $task -and $task.State -eq 'Running'
}

$workerRunning = Get-TaskRunning $taskName
if (-not $workerRunning) {
    Restart-RegisteredTask $taskName '计划任务进程已消失'
    $state.worker_stale = 0
}
else {
    $heartbeatFresh = $false
    if (Test-Path -LiteralPath $heartbeatPath -PathType Leaf) {
        try {
            $heartbeat = Get-Content -LiteralPath $heartbeatPath -Raw | ConvertFrom-Json
            $updated = [DateTimeOffset]::Parse([string]$heartbeat.updated_at)
            $heartbeatFresh = ([DateTimeOffset]::UtcNow - $updated).TotalSeconds -le 180
        }
        catch { $heartbeatFresh = $false }
    }
    if ($heartbeatFresh) {
        $state.worker_stale = 0
    }
    else {
        $state.worker_stale++
        if ($state.worker_stale -ge 2) {
            Restart-RegisteredTask $taskName '心跳连续两次过期'
            $state.worker_stale = 0
        }
    }
}

$apiRunning = Get-TaskRunning $apiTaskName
if (-not $apiRunning) {
    Restart-RegisteredTask $apiTaskName '计划任务进程已消失'
    $state.api_stale = 0
}
else {
    $apiKey = [Environment]::GetEnvironmentVariable(
        'AMAZON_PROCESSOR_API_KEY', 'Machine'
    )
    if (-not $apiKey) {
        $apiKey = [Environment]::GetEnvironmentVariable(
            'AMAZON_PROCESSOR_API_KEY', 'User'
        )
    }
    if (-not $apiKey -and (Test-Path -LiteralPath $envPath -PathType Leaf)) {
        $line = Get-Content -LiteralPath $envPath | Where-Object {
            $_ -match '^\s*AMAZON_PROCESSOR_API_KEY\s*='
        } | Select-Object -Last 1
        if ($line) { $apiKey = ($line -split '=', 2)[1].Trim() }
    }
    $apiHealthy = $false
    if ($apiKey) {
        try {
            $response = Invoke-WebRequest `
                -Uri 'http://127.0.0.1:8765/api/v1/health' `
                -Headers @{ 'X-API-Key' = $apiKey } `
                -TimeoutSec 10 `
                -UseBasicParsing
            $apiHealthy = $response.StatusCode -eq 200
        }
        catch {
            # HTTP 503 means API is alive and only the Worker is degraded.
            if ($_.Exception.Response.StatusCode.value__ -eq 503) {
                $apiHealthy = $true
            }
        }
    }
    if ($apiHealthy) {
        $state.api_stale = 0
    }
    else {
        $state.api_stale++
        if ($state.api_stale -ge 2) {
            Restart-RegisteredTask $apiTaskName '健康检查连续两次失败'
            $state.api_stale = 0
        }
    }
}

$state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
