[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$branch = 'codex/amazon-core-only'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$runtimeRoot = Join-Path $projectRoot '.runtime\server'
$backupRoot = Join-Path $projectRoot '.runtime\update_backups'
$maintenancePath = Join-Path $runtimeRoot 'maintenance.json'
$taskNames = @('AmazonProcessor-Unattended', 'AmazonProcessor-API')
$installedTasks = @()
$env:UV_PROJECT_ENVIRONMENT = Join-Path $env:LOCALAPPDATA 'AmazonProcessor\venv'

New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

function Set-Maintenance([bool]$enabled, [string]$reason) {
    @{
        version = 1
        enabled = $enabled
        reason = $reason
        updated_at = [DateTimeOffset]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content `
        -LiteralPath $maintenancePath `
        -Encoding UTF8
}

function Start-PreviousTasks {
    foreach ($name in $installedTasks) {
        Start-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    }
}

Push-Location $projectRoot
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw '没有找到 Git，无法安全更新。'
    }
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw '没有找到 uv，无法同步外部 Python 环境。'
    }
    $currentBranch = (git branch --show-current).Trim()
    if ($currentBranch -ne $branch) {
        throw "当前分支是 $currentBranch，要求分支为 $branch。"
    }
    $trackedChanges = git status --porcelain --untracked-files=no
    if ($trackedChanges) {
        throw '存在未提交的已跟踪代码修改，请先提交或处理后再更新。'
    }

    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $backupPath = Join-Path $backupRoot "code_$stamp.zip"
    $beforeCommit = (git rev-parse HEAD).Trim()
    $settingsPath = Join-Path $projectRoot 'config\settings.json'
    $settingsHash = if (Test-Path -LiteralPath $settingsPath -PathType Leaf) {
        (Get-FileHash -Algorithm SHA256 -LiteralPath $settingsPath).Hash
    } else { '' }
    git archive --format=zip -o $backupPath HEAD
    if ($LASTEXITCODE -ne 0) { throw '创建更新前代码备份失败。' }
    @{
        version = 1
        branch = $branch
        commit = $beforeCommit
        settings_sha256 = $settingsHash
        backup = $backupPath
        saved_at = [DateTimeOffset]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content `
        -LiteralPath (Join-Path $backupRoot "update_$stamp.json") `
        -Encoding UTF8
    $heartbeatPath = Join-Path $runtimeRoot 'heartbeat.json'
    if (Test-Path -LiteralPath $heartbeatPath -PathType Leaf) {
        Copy-Item -LiteralPath $heartbeatPath `
            -Destination (Join-Path $backupRoot "heartbeat_$stamp.json")
    }

    Set-Maintenance $true '正在执行人工一键更新'
    Start-Sleep -Seconds 20
    uv run python -c `
        "import sys; from amazon_processor.config.locking import processor_is_running; sys.exit(2 if processor_is_running() else 0)"
    if ($LASTEXITCODE -eq 2) {
        throw '当前商品任务仍在处理，请任务完成后再执行更新。'
    }
    if ($LASTEXITCODE -ne 0) {
        throw '无法确认处理器空闲状态，已取消更新。'
    }
    foreach ($name in $taskNames) {
        if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
            $installedTasks += $name
            Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        }
    }

    git fetch origin $branch
    if ($LASTEXITCODE -ne 0) { throw '拉取远程分支失败。' }
    git merge --ff-only "origin/$branch"
    if ($LASTEXITCODE -ne 0) { throw '远程更新不是快进更新，已停止。' }

    uv sync --frozen --quiet
    if ($LASTEXITCODE -ne 0) { throw '外部 Python 环境同步失败。' }
    uv run python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw '离线测试失败。' }
    uv run python -m amazon_processor --help *> $null
    if ($LASTEXITCODE -ne 0) { throw '启动冒烟检查失败。' }

    Set-Maintenance $false ''
    Start-PreviousTasks
    Write-Host "更新完成：$beforeCommit -> $((git rev-parse HEAD).Trim())"
}
catch {
    $message = $_.Exception.Message
    Write-Warning "更新失败：$message"
    if ($backupPath -and (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
        $failedCommit = (git rev-parse HEAD 2>$null).Trim()
        $addedFiles = @()
        if ($beforeCommit -and $failedCommit -and $failedCommit -ne $beforeCommit) {
            $addedFiles = @(git diff --name-only --diff-filter=A $beforeCommit $failedCommit)
            git update-ref "refs/heads/$branch" $beforeCommit
            if ($LASTEXITCODE -ne 0) {
                Write-Warning '无法恢复原分支指针，请人工检查 Git。'
                $addedFiles = @()
            }
        }
        Expand-Archive -LiteralPath $backupPath -DestinationPath $projectRoot -Force
        foreach ($relative in $addedFiles) {
            if (-not $relative) { continue }
            $candidate = [IO.Path]::GetFullPath((Join-Path $projectRoot $relative))
            if (
                $candidate.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar) `
                -and (Test-Path -LiteralPath $candidate -PathType Leaf)
            ) {
                Remove-Item -LiteralPath $candidate -Force
            }
        }
        Write-Warning '已从更新前 ZIP 恢复旧代码文件。'
    }
    Set-Maintenance $false "更新失败并已回滚：$message"
    Start-PreviousTasks
    exit 1
}
finally {
    Pop-Location
}
