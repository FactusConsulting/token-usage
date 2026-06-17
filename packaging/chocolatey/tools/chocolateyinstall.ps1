<#
Chocolatey install script for token-usage.

The release workflow substitutes:
  __VERSION__   - semver of the tag, e.g. 1.4.0
  __CHECKSUM__  - sha256 of the GitHub release source tarball

Public download: this package targets a PUBLIC GitHub release. The zip is
fetched anonymously from the release assets - no token or auth header needed.
#>
$ErrorActionPreference = 'Stop'

$packageName = 'token-usage'
$version     = '__VERSION__'
$checksum    = '__CHECKSUM__'
$tarUrl      = "https://github.com/FactusConsulting/token-usage/releases/download/v$version/token-usage-$version.zip"

$toolsDir    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$extractDir  = Join-Path $toolsDir 'src'

# Install-ChocolateyZipPackage downloads + verifies + extracts the zip.
# The release is public, so the asset downloads anonymously - no auth header.
$args = @{
    PackageName    = $packageName
    UnzipLocation  = $extractDir
    Url            = $tarUrl
    Checksum       = $checksum
    ChecksumType   = 'sha256'
}
Install-ChocolateyZipPackage @args

# Locate the unpacked tree (release tarball top level may or may not be nested).
$repoRoot = $extractDir
$nested   = Get-ChildItem -LiteralPath $extractDir -Directory | Select-Object -First 1
if ($nested -and (Test-Path -LiteralPath (Join-Path $nested.FullName 'installers\windows\install.ps1'))) {
    $repoRoot = $nested.FullName
}

$installer = Join-Path $repoRoot 'installers\windows\install.ps1'
if (-not (Test-Path -LiteralPath $installer)) {
    throw "Could not find installers\windows\install.ps1 under $extractDir"
}

Write-Host "[choco] running $installer with RepoRoot=$repoRoot"
& powershell.exe -ExecutionPolicy Bypass -File $installer -RepoRoot $repoRoot
if ($LASTEXITCODE -ne 0) {
    throw "install.ps1 exited $LASTEXITCODE"
}
