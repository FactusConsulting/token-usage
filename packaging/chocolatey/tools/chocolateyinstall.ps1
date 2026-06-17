<#
Chocolatey install script for token-usage.

The release workflow substitutes:
  __VERSION__   — semver of the tag, e.g. 1.4.0
  __CHECKSUM__  — sha256 of the GitHub release source tarball

Private-repo download: this package targets a PRIVATE GitHub release.
Install-ChocolateyZipPackage will follow the URL using whatever HTTP headers
are available to it; for a private repo the end user must export
$env:GITHUB_TOKEN (a PAT with `repo` read) before running `choco install`.
The package treats the token as REQUIRED — there is no anonymous fallback.

Alternative: host this .nupkg on a private Chocolatey feed and pre-stage the
release tarball where the feed can reach it; then GITHUB_TOKEN is not needed
on each client.
#>
$ErrorActionPreference = 'Stop'

$packageName = 'token-usage'
$version     = '__VERSION__'
$checksum    = '__CHECKSUM__'
$tarUrl      = "https://github.com/FactusConsulting/token-usage/releases/download/v$version/token-usage-$version.zip"

$toolsDir    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$extractDir  = Join-Path $toolsDir 'src'

if (-not $env:GITHUB_TOKEN) {
    throw "token-usage is a PRIVATE package. Set `$env:GITHUB_TOKEN to a PAT with 'repo' read before running 'choco install'."
}

# Install-ChocolateyZipPackage downloads + verifies + extracts the zip.
# We pass the token via -Options so the request to the private release works.
$args = @{
    PackageName    = $packageName
    UnzipLocation  = $extractDir
    Url            = $tarUrl
    Checksum       = $checksum
    ChecksumType   = 'sha256'
    Options        = @{ Headers = @{ Authorization = "token $env:GITHUB_TOKEN"; Accept = 'application/octet-stream' } }
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
