[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$taskName = 'AmazonProcessor-Unattended'
$apiTaskName = 'AmazonProcessor-API'
$watchdogTaskName = 'AmazonProcessor-Watchdog'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$launcher = Join-Path $PSScriptRoot '启动全天处理.bat'
$apiLauncher = Join-Path $PSScriptRoot '启动任务接口.bat'
$watchdog = Join-Path $PSScriptRoot '后台服务看门狗.ps1'
$runtimeRoot = Join-Path $projectRoot '.runtime\server'
$systemSettingsPath = Join-Path $runtimeRoot 'system_settings.json'

$currentPrincipal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
$isAdministrator = $currentPrincipal.IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)
if (-not $isAdministrator) {
    $powershell = Join-Path $env:SystemRoot `
        'System32\WindowsPowerShell\v1.0\powershell.exe'
    $arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($Uninstall) { $arguments += ' -Uninstall' }
    $process = Start-Process `
        -FilePath $powershell `
        -ArgumentList $arguments `
        -Verb RunAs `
        -Wait `
        -PassThru
    exit $process.ExitCode
}

if ($Uninstall) {
    foreach ($name in @($taskName, $apiTaskName, $watchdogTaskName)) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $name -Confirm:$false
            Write-Host "已删除计划任务：$name"
        }
    }
    if (Test-Path -LiteralPath $systemSettingsPath -PathType Leaf) {
        try {
            $savedSettings = Get-Content `
                -LiteralPath $systemSettingsPath `
                -Raw | ConvertFrom-Json
            if ($null -ne $savedSettings.ac_sleep_seconds) {
                powercfg /setacvalueindex `
                    SCHEME_CURRENT SUB_SLEEP STANDBYIDLE `
                    ([int64]$savedSettings.ac_sleep_seconds) | Out-Null
                powercfg /setactive SCHEME_CURRENT | Out-Null
                Write-Host '已恢复安装前的插电睡眠设置。'
            }
        }
        catch {
            Write-Warning "恢复插电睡眠设置失败：$($_.Exception.Message)"
        }
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "找不到 Worker 启动器：$launcher"
}
if (-not (Test-Path -LiteralPath $watchdog -PathType Leaf)) {
    throw "找不到 Worker 看门狗：$watchdog"
}
if (-not (Test-Path -LiteralPath $apiLauncher -PathType Leaf)) {
    throw "找不到任务 API 启动器：$apiLauncher"
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env') -PathType Leaf)) {
    throw '项目根目录缺少 .env，请先打开“00_常用入口\03_配置与模型.bat”配置 API 密钥。'
}

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath $systemSettingsPath -PathType Leaf)) {
    $query = powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 2>&1
    $indexLine = $query | Where-Object {
        $_ -match 'AC Power Setting Index|交流电源设置索引|AC 电源设置索引'
    } | Select-Object -Last 1
    if (-not $indexLine) {
        $indexLine = $query | Where-Object { $_ -match '0x[0-9a-fA-F]{8}' } |
            Select-Object -Last 1
    }
    $sleepSeconds = $null
    if ($indexLine -match '0x([0-9a-fA-F]+)') {
        $sleepSeconds = [Convert]::ToInt64($Matches[1], 16)
    }
    @{
        version = 1
        saved_at = [DateTimeOffset]::UtcNow.ToString('o')
        ac_sleep_seconds = $sleepSeconds
    } | ConvertTo-Json | Set-Content `
        -LiteralPath $systemSettingsPath `
        -Encoding UTF8
}
powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0 | Out-Null
powercfg /setactive SCHEME_CURRENT | Out-Null

$envPath = Join-Path $projectRoot '.env'
$systemClientKey = [Environment]::GetEnvironmentVariable(
    'AMAZON_PROCESSOR_API_KEY',
    'Machine'
)
$userClientKey = [Environment]::GetEnvironmentVariable(
    'AMAZON_PROCESSOR_API_KEY',
    'User'
)
$fileHasClientKey = Select-String `
    -LiteralPath $envPath `
    -Pattern '^\s*AMAZON_PROCESSOR_API_KEY\s*=\s*\S+' `
    -Quiet
if (-not $systemClientKey -and -not $userClientKey -and -not $fileHasClientKey) {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    $clientKey = 'ap_' + (-join ($bytes | ForEach-Object { $_.ToString('x2') }))
    Add-Content -LiteralPath $envPath -Encoding UTF8 `
        -Value "`nAMAZON_PROCESSOR_API_KEY=$clientKey"
    Write-Host '已在 .env 中生成独立的调用方 API 密钥。'
}

$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\cmd.exe" `
    -Argument "/d /c `"`"$launcher`"`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType S4U `
    -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

$task = New-ScheduledTask `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Amazon 采集表无人值守处理 Worker'

Register-ScheduledTask -TaskName $taskName -InputObject $task -Force | Out-Null

$apiAction = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\cmd.exe" `
    -Argument "/d /c `"`"$apiLauncher`"`""
$apiTask = New-ScheduledTask `
    -Action $apiAction `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Amazon 采集表异步任务 API（仅本机监听）'
Register-ScheduledTask `
    -TaskName $apiTaskName `
    -InputObject $apiTask `
    -Force | Out-Null

$watchdogAction = New-ScheduledTaskAction `
    -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$watchdog`""
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$watchdogTask = New-ScheduledTask `
    -Action $watchdogAction `
    -Trigger $watchdogTrigger `
    -Principal $principal `
    -Settings $settings `
    -Description 'Amazon Worker 心跳看门狗'
Register-ScheduledTask `
    -TaskName $watchdogTaskName `
    -InputObject $watchdogTask `
    -Force | Out-Null
Start-ScheduledTask -TaskName $taskName
Start-ScheduledTask -TaskName $apiTaskName

Write-Host "已安装并启动：$taskName"
Write-Host "已安装并启动：$apiTaskName"
Write-Host "已安装看门狗：$watchdogTaskName"
Write-Host "操作员目录：$(Join-Path $projectRoot 'Amazon日常操作')"
Write-Host "交付目录：$(Join-Path $projectRoot '02_处理结果\服务器交付')"
Write-Host '日常查看：00_常用入口\05_查看系统状态.bat'
Write-Host '接口地址：http://127.0.0.1:8765/api/v1'
