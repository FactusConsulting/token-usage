<#
Uninstall script for token-usage.

Removes the Scheduled Task and the install dir under %LOCALAPPDATA%. Leaves
node + python alone (other packages may depend on them). The .env stays in
$installDir until the dir is removed - there's nothing secret-rotation can
do here that a user couldn't do by hand, so we just nuke it.
#>
$ErrorActionPreference = 'Continue'

$taskName  = 'TokenUsageCcusageShip'
$installDir = Join-Path $env:LOCALAPPDATA 'token-usage'

$existing = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "[choco] unregistering Scheduled Task $taskName"
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
} else {
    Write-Host "[choco] no Scheduled Task $taskName to remove"
}

if (Test-Path -LiteralPath $installDir) {
    Write-Host "[choco] removing $installDir"
    Remove-Item -LiteralPath $installDir -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "[choco] token-usage uninstall complete. ccusage (npm -g) and node were left in place."
