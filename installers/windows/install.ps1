<#
.SYNOPSIS
    Install ccusage + the token-usage shim on Windows 11 and register an
    hourly Scheduled Task.

.DESCRIPTION
    Idempotent. Re-running this script reconciles:
      * Node LTS (via winget, skipped if already installed)
      * ccusage (npm -g, upgraded to latest)
      * shim/ccusage-ship.py copied to %LOCALAPPDATA%\token-usage\
      * Python deps installed into a venv at %LOCALAPPDATA%\token-usage\.venv
      * .env created with placeholder values if missing (user fills it in)
      * Scheduled Task "TokenUsageCcusageShip" running hourly as the current
        user, even when the user is not logged in

    After install, edit %LOCALAPPDATA%\token-usage\.env and fill in the real
    LANGFUSE_* keys.

.PARAMETER RepoRoot
    Where this repo is checked out. Defaults to the parent of this script's
    parent, so you can run from a fresh `git clone` without arguments.
#>

[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$installDir = Join-Path $env:LOCALAPPDATA 'token-usage'
$shimSrc    = Join-Path $RepoRoot 'shim\ccusage-ship.py'
$envExample = Join-Path $RepoRoot 'shim\.env.example'
$reqFile    = Join-Path $RepoRoot 'shim\requirements.txt'

if (-not (Test-Path $shimSrc)) {
    throw "Expected to find $shimSrc - pass -RepoRoot to point at the checkout."
}

Write-Host "[install] target dir: $installDir"
New-Item -ItemType Directory -Force -Path $installDir | Out-Null

# 1. Node LTS via winget (skipped if already installed).
$nodeCmd = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCmd) {
    Write-Host "[install] installing Node.js LTS via winget..."
    winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
    # winget doesn't refresh PATH for the current session.
    $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                [System.Environment]::GetEnvironmentVariable('Path','User')
} else {
    Write-Host "[install] node already present: $($nodeCmd.Source)"
}

# 2. ccusage via npm -g, pinned to the version in CCUSAGE_VERSION at repo root.
# Packagers (choco / brew / nix) all read this same file, so a single bump
# propagates everywhere.
$ccusageVersionFile = Join-Path $RepoRoot 'CCUSAGE_VERSION'
if (Test-Path $ccusageVersionFile) {
    $ccusageVersion = (Get-Content -Raw $ccusageVersionFile).Trim()
    Write-Host "[install] (re)installing ccusage@$ccusageVersion globally via npm..."
    & npm install -g "ccusage@$ccusageVersion" | Out-Host
} else {
    Write-Host "[install] CCUSAGE_VERSION not found, installing latest ccusage..."
    & npm install -g ccusage | Out-Host
}

# 3. Shim files.
Copy-Item -Force $shimSrc (Join-Path $installDir 'ccusage-ship.py')

$envTarget = Join-Path $installDir '.env'
if (-not (Test-Path $envTarget)) {
    Copy-Item -Force $envExample $envTarget
    Write-Warning "[install] wrote placeholder .env at $envTarget - edit it to set LANGFUSE_* keys."
} else {
    Write-Host "[install] existing .env preserved at $envTarget"
}

# 4. Python venv + deps. Prefer py -3 launcher; fall back to python.
$venvDir = Join-Path $installDir '.venv'
$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pyLauncher) {
    $pyLauncher = Get-Command python -ErrorAction SilentlyContinue
}
if (-not $pyLauncher) {
    throw "No python found on PATH. Install Python 3.10+ (py launcher recommended) and re-run."
}
if (-not (Test-Path (Join-Path $venvDir 'Scripts\python.exe'))) {
    Write-Host "[install] creating Python venv at $venvDir"
    & $pyLauncher.Source -3 -m venv $venvDir
}
$venvPython = Join-Path $venvDir 'Scripts\python.exe'
& $venvPython -m pip install --upgrade pip | Out-Host
& $venvPython -m pip install -r $reqFile | Out-Host

# 5. Wrapper batch file that loads the venv, runs the shim, captures output.
$wrapper = Join-Path $installDir 'run-ship.cmd'
@"
@echo off
setlocal
set TOKEN_USAGE_DIR=%LOCALAPPDATA%\token-usage
set LOG=%TOKEN_USAGE_DIR%\ship.log
rem Rotate so the log can't grow without bound: keep one previous (~1 MB each).
if exist "%LOG%" for %%F in ("%LOG%") do if %%~zF GTR 1048576 move /Y "%LOG%" "%LOG%.1" >nul
"%TOKEN_USAGE_DIR%\.venv\Scripts\python.exe" "%TOKEN_USAGE_DIR%\ccusage-ship.py" >> "%LOG%" 2>&1
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $wrapper -Encoding ASCII

# 6. Scheduled Task - hourly, runs even when user is logged out (S4U logon).
$taskName = 'TokenUsageCcusageShip'
$action = New-ScheduledTaskAction -Execute $wrapper
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
            -RepetitionInterval (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType S4U -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[install] Scheduled Task '$taskName' registered (hourly)."
Write-Host "[install] Done. Edit $envTarget then either wait an hour or run:"
Write-Host "         Start-ScheduledTask -TaskName $taskName"
