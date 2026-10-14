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
      * token-usage.cmd entry point, with the install dir added to the user PATH
      * Scheduled Task "TokenUsageCcusageShip" running hourly as the current
        user (Interactive logon), launched via pythonw so no console window
        flashes on each run

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

# npm is the one that ships beside the node on PATH. A version manager can leave
# node without it (nvm-windows recreating a version folder that holds only
# node.exe), and Chocolatey's process may not have npm's directory on PATH even
# when it exists - so look next to node before giving up, and say which node
# is broken instead of failing on a bare "npm is not recognized".
$npmCmd = Get-Command npm -ErrorAction SilentlyContinue
if ($npmCmd) {
    $npm = $npmCmd.Source
} else {
    $node = (Get-Command node -ErrorAction SilentlyContinue).Source
    $npm = if ($node) { Join-Path (Split-Path $node) 'npm.cmd' }
    if (-not $npm -or -not (Test-Path $npm)) {
        throw ("npm not found: the node on PATH ($node) has no npm beside it. " +
               "Repair that Node install (with nvm-windows: 'nvm install <version>' " +
               "then 'nvm use <version>' from an elevated shell), then re-run.")
    }
}
Write-Host "[install] npm: $npm"

# npm and pip write notices (an available update, a deprecation) to stderr. Under
# ErrorActionPreference=Stop, Windows PowerShell turns any native stderr line into a
# terminating error - so a successful install would abort on "npm notice". Run them
# with Continue and judge by the exit code instead.
function Invoke-Native([string]$What, [scriptblock]$Command) {
    $saved = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Command 2>&1 | ForEach-Object { "$_" } | Out-Host }
    finally { $ErrorActionPreference = $saved }
    if ($LASTEXITCODE -ne 0) {
        throw "$What failed (exit $LASTEXITCODE); see the output above."
    }
}

# 2. ccusage via npm -g, pinned to the version in CCUSAGE_VERSION at repo root.
# Packagers (choco / brew / nix) all read this same file, so a single bump
# propagates everywhere.
$ccusageVersionFile = Join-Path $RepoRoot 'CCUSAGE_VERSION'
if (Test-Path $ccusageVersionFile) {
    $ccusageVersion = (Get-Content -Raw $ccusageVersionFile).Trim()
    Write-Host "[install] (re)installing ccusage@$ccusageVersion globally via npm..."
    Invoke-Native 'npm install -g ccusage' { & $npm install -g "ccusage@$ccusageVersion" --no-fund --no-audit }
} else {
    Write-Host "[install] CCUSAGE_VERSION not found, installing latest ccusage..."
    Invoke-Native 'npm install -g ccusage' { & $npm install -g ccusage --no-fund --no-audit }
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
$venvPythonw = Join-Path $venvDir 'Scripts\pythonw.exe'
Invoke-Native 'pip install --upgrade pip' { & $venvPython -m pip install --upgrade pip }
Invoke-Native 'pip install -r requirements.txt' { & $venvPython -m pip install -r $reqFile }

# 5. Windowless launcher. A .pyw run by the venv's pythonw.exe leaves no console
# window when the Scheduled Task fires (a .cmd or python.exe would flash one
# every hour under Interactive logon). It rotates ship.log and captures the
# shim's output; the shim itself spawns ccusage with CREATE_NO_WINDOW, so the
# whole chain is silent.
$wrapper = Join-Path $installDir 'run-ship.pyw'
@'
"""Windowless launcher for the hourly Scheduled Task.

Run by pythonw.exe so no console window appears. Rotates ship.log (keep one
~1 MB previous), then runs the shim with its stdout/stderr captured into the
log. The shim spawns ccusage with CREATE_NO_WINDOW, keeping the chain silent.
"""
import os
import subprocess

base = os.path.join(os.environ["LOCALAPPDATA"], "token-usage")
log = os.path.join(base, "ship.log")
if os.path.exists(log) and os.path.getsize(log) > 1_048_576:
    os.replace(log, log + ".1")
with open(log, "ab") as fh:
    subprocess.run(
        [os.path.join(base, ".venv", "Scripts", "pythonw.exe"),
         os.path.join(base, "ccusage-ship.py")],
        stdout=fh, stderr=fh, check=False,
    )
'@ | Set-Content -Path $wrapper -Encoding UTF8

# A stale run-ship.cmd from an older install would still work, but the task no
# longer points at it; drop it so there is one obvious entry point.
$staleCmd = Join-Path $installDir 'run-ship.cmd'
if (Test-Path $staleCmd) { Remove-Item -Force $staleCmd }

# 6. `token-usage` entry point. The Homebrew and Nix channels expose one, and
# the docs tell every user to run `token-usage --dry-run`; without this the
# command simply doesn't exist on Windows. Uses python.exe (not pythonw) so an
# interactive run prints to the console the user typed it in.
$cliCmd = Join-Path $installDir 'token-usage.cmd'
@"
@echo off
rem token-usage CLI entry point. Interactive counterpart to the hourly
rem Scheduled Task, which runs run-ship.pyw via pythonw instead.
"$venvPython" "$(Join-Path $installDir 'ccusage-ship.py')" %*
exit /b %ERRORLEVEL%
"@ | Set-Content -Path $cliCmd -Encoding ASCII

# Put the install dir on the user PATH so `token-usage` resolves in new shells.
# Idempotent: re-running must not append a duplicate entry.
$userPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
if ([string]::IsNullOrEmpty($userPath)) { $userPath = '' }
$onPath = $userPath.Split(';') | Where-Object { $_.TrimEnd('\') -ieq $installDir.TrimEnd('\') }
if (-not $onPath) {
    $newPath = if ($userPath.Trim()) { "$($userPath.TrimEnd(';'));$installDir" } else { $installDir }
    [System.Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    Write-Host "[install] added $installDir to the user PATH (open a new shell to pick it up)."
} else {
    Write-Host "[install] $installDir already on the user PATH."
}

# 7. Scheduled Task - hourly, Interactive logon. S4U ("run whether logged on or
# not") silently never starts for a standard (non-admin) user, so the task
# would register yet never ship. Interactive runs reliably while the user is
# logged in; pythonw keeps it silent. RepetitionDuration must be set explicitly
# or the hourly repetition is treated as already expired and never fires.
$taskName = 'TokenUsageCcusageShip'
$action = New-ScheduledTaskAction -Execute $venvPythonw -Argument "`"$wrapper`""
$trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
            -RepetitionInterval (New-TimeSpan -Hours 1) `
            -RepetitionDuration (New-TimeSpan -Days 3650)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "[install] Scheduled Task '$taskName' registered (hourly, silent)."
Write-Host "[install] Done. Edit $envTarget then either wait an hour or run:"
Write-Host "         token-usage --dry-run          # new shell; print, don't send"
Write-Host "         Start-ScheduledTask -TaskName $taskName"
