<#
Chocolatey install script for ccusage.

scripts/render-chocolatey.ps1 substitutes the placeholders from
ccusage.lock.json: the npm tarball URL and its sha512 (hex) for each
architecture.

The tarball holds package/bin/ccusage.exe, a static executable. It ends up in
this package's tools dir, and Chocolatey shims every .exe in the package dir
into its bin dir, so `ccusage` is on PATH afterwards. chocolateyuninstall is
not needed: removing the package removes the exe and its shim.
#>
$ErrorActionPreference = 'Stop'

$version  = '__CCUSAGE_VERSION__'
$toolsDir = Split-Path -Parent $MyInvocation.MyCommand.Definition

$binaries = @{
    x64   = @{ Url = '__URL_WIN32_X64__';   Checksum = '__SHA512_WIN32_X64__' }
    arm64 = @{ Url = '__URL_WIN32_ARM64__'; Checksum = '__SHA512_WIN32_ARM64__' }
}
# A 32-bit host process on ARM64 reports x86 in PROCESSOR_ARCHITECTURE and the
# real architecture in PROCESSOR_ARCHITEW6432.
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { 'arm64' } else { 'x64' }
$binary = $binaries[$arch]

$tgz = Join-Path $toolsDir "ccusage-$version-$arch.tgz"
Get-ChocolateyWebFile -PackageName 'ccusage' -FileFullPath $tgz `
    -Url $binary.Url -Checksum $binary.Checksum -ChecksumType 'sha512' | Out-Null

# tar.exe ships with Windows 10 1803 and later and reads .tgz directly.
$unpack = Join-Path $toolsDir 'unpack'
New-Item -ItemType Directory -Force -Path $unpack | Out-Null
& "$env:SystemRoot\System32\tar.exe" -xzf $tgz -C $unpack
if ($LASTEXITCODE -ne 0) { throw "tar.exe could not unpack $tgz (exit $LASTEXITCODE)" }

$exe = Join-Path $toolsDir 'ccusage.exe'
Move-Item -Force -LiteralPath (Join-Path $unpack 'package\bin\ccusage.exe') -Destination $exe
Remove-Item -Recurse -Force -LiteralPath $unpack, $tgz

$reported = ((& $exe --version) -split '\s+')[-1]
if ($reported -ne $version) {
    throw "ccusage.exe reports '$reported', expected $version"
}
Write-Host "[choco] ccusage $version ($arch) installed: $exe"
