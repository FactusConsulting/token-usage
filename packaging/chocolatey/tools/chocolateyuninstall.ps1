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

# Drop the shim chocolateyinstall.ps1 generated in Chocolatey's bin dir.
Uninstall-BinFile -Name 'token-usage'

# install.ps1 also appended $installDir to the user PATH; take it back out so an
# uninstall doesn't leave a dangling entry.
$userPath = [System.Environment]::GetEnvironmentVariable('Path', 'User')
if ($userPath) {
    $kept = $userPath.Split(';') |
        Where-Object { $_ -and $_.TrimEnd('\') -ine $installDir.TrimEnd('\') }
    $newPath = ($kept -join ';')
    if ($newPath -ne $userPath) {
        Write-Host "[choco] removing $installDir from the user PATH"
        [System.Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
    }
}

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
