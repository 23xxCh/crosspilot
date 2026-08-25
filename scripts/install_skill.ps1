$ErrorActionPreference = 'Stop'

$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$skillsRoot = Join-Path $env:USERPROFILE '.codex\skills'
$target = Join-Path $skillsRoot 'amazon-json-processor'

New-Item -ItemType Directory -Path $skillsRoot -Force | Out-Null

if (Test-Path -LiteralPath $target) {
    $existing = Get-Item -LiteralPath $target -Force
    $resolvedTarget = (Resolve-Path -LiteralPath $target).Path
    if ($resolvedTarget -eq $projectRoot) {
        Write-Host "Skill is already registered: $target"
        exit 0
    }
    throw "Target exists and does not point to this project: $target"
}

New-Item -ItemType Junction -Path $target -Target $projectRoot | Out-Null
Write-Host "Skill registered: $target -> $projectRoot"
