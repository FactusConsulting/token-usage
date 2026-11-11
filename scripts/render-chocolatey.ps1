<#
.SYNOPSIS
    Render and pack both Chocolatey packages: ccusage and token-usage.

.DESCRIPTION
    Fills the placeholders in packaging/chocolatey-ccusage and
    packaging/chocolatey from ccusage.lock.json and the arguments, on a copy
    of each so the checkout stays a template, then runs `choco pack` into
    -OutDir. The release job and the Windows CI test both use this script, so
    CI tests exactly what a release publishes.

    Writes the two .nupkg paths to stdout, ccusage first: a feed has to carry
    the dependency before (or with) the package that needs it.

.PARAMETER Version
    token-usage package version, e.g. 0.2.7.
.PARAMETER ZipUrl
    URL of the token-usage source zip; a local path works too (CI).
.PARAMETER ZipChecksum
    sha256 of that zip, hex.
.PARAMETER OutDir
    Where the .nupkg files go.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)] [string]$Version,
    [Parameter(Mandatory)] [string]$ZipUrl,
    [Parameter(Mandatory)] [string]$ZipChecksum,
    [Parameter(Mandatory)] [string]$OutDir
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$lock = Get-Content -Raw (Join-Path $repoRoot 'ccusage.lock.json') | ConvertFrom-Json
$pinned = (Get-Content -Raw (Join-Path $repoRoot 'CCUSAGE_VERSION')).Trim()
if ($lock.version -ne $pinned) {
    throw "ccusage.lock.json pins $($lock.version), CCUSAGE_VERSION pins $pinned"
}

# npm's integrity is SRI: "sha512-" + base64. Chocolatey wants hex.
function ConvertTo-HexSha512([string]$Integrity) {
    if (-not $Integrity.StartsWith('sha512-')) { throw "not a sha512 integrity: $Integrity" }
    $bytes = [Convert]::FromBase64String($Integrity.Substring(7))
    ($bytes | ForEach-Object { $_.ToString('x2') }) -join ''
}

$values = @{
    '__VERSION__'               = $Version
    '__URL__'                   = $ZipUrl
    '__CHECKSUM__'              = $ZipChecksum
    '__CCUSAGE_VERSION__'       = $lock.version
    '__URL_WIN32_X64__'         = $lock.platforms.'win32-x64'.url
    '__SHA512_WIN32_X64__'      = ConvertTo-HexSha512 $lock.platforms.'win32-x64'.integrity
    '__URL_WIN32_ARM64__'       = $lock.platforms.'win32-arm64'.url
    '__SHA512_WIN32_ARM64__'    = ConvertTo-HexSha512 $lock.platforms.'win32-arm64'.integrity
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
$OutDir = (Resolve-Path $OutDir).Path
$work = Join-Path ([IO.Path]::GetTempPath()) "choco-render-$([guid]::NewGuid())"

try {
    foreach ($pkg in @(
        @{ Dir = 'chocolatey-ccusage'; Nuspec = 'ccusage.nuspec'; Id = 'ccusage'; Ver = $lock.version },
        @{ Dir = 'chocolatey'; Nuspec = 'token-usage.nuspec'; Id = 'token-usage'; Ver = $Version }
    )) {
        $dst = Join-Path $work $pkg.Dir
        Copy-Item -Recurse (Join-Path $repoRoot "packaging\$($pkg.Dir)") $dst
        Get-ChildItem -Recurse -File $dst -Include *.nuspec, *.ps1, *.txt | ForEach-Object {
            $text = Get-Content -Raw $_.FullName
            foreach ($k in $values.Keys) { $text = $text.Replace($k, $values[$k]) }
            if ($text -match '__[A-Z0-9_]+__') { throw "unfilled placeholder $($Matches[0]) in $($_.Name)" }
            Set-Content -NoNewline -Path $_.FullName -Value $text
        }
        choco pack (Join-Path $dst $pkg.Nuspec) --out $OutDir | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "choco pack $($pkg.Id) failed" }
        $nupkg = Join-Path $OutDir "$($pkg.Id).$($pkg.Ver).nupkg"
        if (-not (Test-Path $nupkg)) { throw "choco pack produced no $nupkg" }
        $nupkg
    }
}
finally {
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
