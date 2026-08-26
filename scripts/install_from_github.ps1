param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'AmazonJsonProcessor'),
    [string]$Repository = 'https://github.com/23xxCh/crosspilot.git',
    [string]$Branch = 'main'
)

$ErrorActionPreference = 'Stop'

function Find-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }
    return $null
}

if (-not $env:LOCALAPPDATA) {
    throw 'LOCALAPPDATA is unavailable. Run this installer from a normal Windows user account.'
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is required. Install Git for Windows, then run this command again.'
}

if (Test-Path -LiteralPath $InstallRoot) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRoot '.git') -PathType Container)) {
        throw "Install path already exists but is not a Git checkout: $InstallRoot"
    }
    $dirty = & git -C $InstallRoot status --porcelain
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to inspect the existing installation.'
    }
    if ($dirty) {
        throw "Existing installation has local changes. Preserve them before updating: $InstallRoot"
    }
    & git -C $InstallRoot fetch origin $Branch
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to fetch the latest Skill version.'
    }
    & git -C $InstallRoot checkout $Branch
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to switch to branch: $Branch"
    }
    & git -C $InstallRoot pull --ff-only origin $Branch
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to fast-forward the existing installation.'
    }
} else {
    $parent = Split-Path -Parent $InstallRoot
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    & git clone --depth 1 --branch $Branch --single-branch $Repository $InstallRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Unable to download the Amazon JSON Processor Skill.'
    }
}

$uv = Find-Uv
if (-not $uv) {
    $uvInstaller = Join-Path $env:TEMP "uv-installer-$PID.ps1"
    try {
        Invoke-WebRequest -UseBasicParsing -Uri 'https://astral.sh/uv/install.ps1' -OutFile $uvInstaller
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $uvInstaller
        if ($LASTEXITCODE -ne 0) {
            throw 'The official uv installer failed.'
        }
    } finally {
        Remove-Item -LiteralPath $uvInstaller -Force -ErrorAction SilentlyContinue
    }
    $uv = Find-Uv
    if (-not $uv) {
        throw 'uv was installed but could not be located. Open a new PowerShell window and run the command again.'
    }
}

Push-Location $InstallRoot
try {
    & $uv sync --frozen
    if ($LASTEXITCODE -ne 0) {
        throw 'Dependency synchronization failed.'
    }

    $envPath = Join-Path $InstallRoot '.env'
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath (Join-Path $InstallRoot '.env.example') -Destination $envPath
    }

    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $InstallRoot 'scripts\install_skill.ps1')
    if ($LASTEXITCODE -ne 0) {
        throw 'Skill registration failed.'
    }
} finally {
    Pop-Location
}

Write-Host ''
Write-Host "Installed: $InstallRoot"
Write-Host "Next: add DEEPSEEK_KEY to $InstallRoot\.env, restart Codex, then provide an Amazon JSON file to the Agent."
