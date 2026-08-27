param(
    [Parameter(Mandatory = $true)]
    [string]$StagingDirectory,

    [switch]$UnsignedEngineeringCandidate,

    [string]$SignToolPath,
    [string]$SigningCertificateThumbprint,
    [string]$SigningCertificateSubject,
    [string]$TimestampUrl
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Read-JsonFile {
    param([string]$RelativePath)
    $path = Join-Path $repoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path)) { throw "missing release input: $RelativePath" }
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Read-TextFile {
    param([string]$RelativePath)
    $path = Join-Path $repoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path)) { throw "missing release input: $RelativePath" }
    return Get-Content -LiteralPath $path -Raw
}

function Get-LockRootVersion {
    param([string]$RelativePath)
    $text = Read-TextFile $RelativePath
    if ($text -notmatch '(?s)"packages"\s*:\s*\{\s*""\s*:\s*\{.*?"version"\s*:\s*"([^"]+)"') {
        throw "version drift: cannot read root package version from $RelativePath"
    }
    return $Matches[1]
}

function Get-LockTopVersion {
    param([string]$RelativePath)
    $text = Read-TextFile $RelativePath
    if ($text -notmatch '(?m)^\s*"version"\s*:\s*"([^"]+)"\s*,') {
        throw "version drift: cannot read top package-lock version from $RelativePath"
    }
    return $Matches[1]
}

function Assert-VersionSync {
    $version = Read-JsonFile 'release\version.json'
    if ($version.version -ne '0.6.0') { throw "version drift: release/version.json" }
    if ($version.build_id -notmatch '^0\.6\.0\+[0-9]{8}\.[0-9]{6}\.[0-9a-f]{7,40}$') { throw "version drift: invalid build_id" }

    $rootPackage = Read-JsonFile 'package.json'
    $desktopPackage = Read-JsonFile 'desktop\package.json'
    $tauri = Read-JsonFile 'desktop\src-tauri\tauri.conf.json'
    $cargoToml = Read-TextFile 'desktop\src-tauri\Cargo.toml'

    if ($rootPackage.version -ne $version.version) { throw "version drift: package.json" }
    if ((Get-LockTopVersion 'package-lock.json') -ne $version.version -or (Get-LockRootVersion 'package-lock.json') -ne $version.version) { throw "version drift: package-lock.json" }
    if ($desktopPackage.version -ne $version.version) { throw "version drift: desktop/package.json" }
    if ((Get-LockTopVersion 'desktop\package-lock.json') -ne $version.version -or (Get-LockRootVersion 'desktop\package-lock.json') -ne $version.version) { throw "version drift: desktop/package-lock.json" }
    if ($tauri.version -ne $version.version) { throw "version drift: tauri.conf.json" }
    if ($cargoToml -notmatch '(?m)^version = "0\.6\.0"$') { throw "version drift: Cargo.toml" }
    return $version
}

function Assert-PythonLocksReady {
    foreach ($relative in @('requirements.lock', 'requirements-build.lock')) {
        $text = Read-TextFile $relative
        if ($text -match '(?m)^# LOCK-STATUS: BLOCKED$') {
            throw "Python lock BLOCKED: $relative lacks hash-complete offline distribution evidence"
        }
        if ($text -notmatch '(?m)^--require-hashes$') {
            throw "Python lock missing --require-hashes: $relative"
        }
        if ($text -notmatch '--hash=sha256:[0-9a-f]{64}') {
            throw "Python lock missing real hashes: $relative"
        }
    }
}

function Assert-CleanStaging {
    param([string]$Path)
    $forbiddenPatterns = @(
        'config.local.json',
        '*.secure.json',
        '*.sqlite',
        '*.db',
        '*.cookie',
        '*.cookies',
        '*credential*',
        '*secret*',
        '*token*'
    )
    if (-not (Test-Path -LiteralPath $Path)) { return }
    foreach ($pattern in $forbiddenPatterns) {
        $hit = Get-ChildItem -LiteralPath $Path -Recurse -Force -File -Filter $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $hit) { throw "forbidden release input in staging: $($hit.Name)" }
    }
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-Sha256 {
    param([string]$Path)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $stream = [IO.File]::OpenRead($Path)
        try {
            return (($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') }) -join '')
        } finally {
            $stream.Dispose()
        }
    } finally {
        $sha.Dispose()
    }
}

function Write-Sha256Manifest {
    param([string]$Path)
    $lines = @()
    $files = Get-ChildItem -LiteralPath $Path -Recurse -Force -File |
        Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
        Sort-Object FullName
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Path.Length).TrimStart('\') -replace '\\', '/'
        $lines += "$(Get-Sha256 $file.FullName)  $relative"
    }
    Write-Utf8NoBom (Join-Path $Path 'SHA256SUMS.txt') (($lines -join "`n") + "`n")
}

$stagePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StagingDirectory)
[IO.Directory]::CreateDirectory($stagePath) | Out-Null
Assert-CleanStaging $stagePath
$version = Assert-VersionSync
Assert-PythonLocksReady

if (-not $UnsignedEngineeringCandidate) {
    if (-not $SignToolPath) { throw 'signing BLOCKED: SignToolPath is required' }
    if (-not $TimestampUrl) { throw 'timestamp BLOCKED: TimestampUrl is required' }
    if (-not $SigningCertificateThumbprint -and -not $SigningCertificateSubject) {
        throw 'signing BLOCKED: explicit certificate thumbprint or subject is required'
    }
}

$artifactName = "$($version.artifact_name_prefix)-windows-source.zip"
$artifactPath = Join-Path $stagePath $artifactName
$sourceFiles = @(
    'release\version.json',
    'package.json',
    'package-lock.json',
    'desktop\package.json',
    'desktop\package-lock.json',
    'desktop\src-tauri\Cargo.toml',
    'desktop\src-tauri\Cargo.lock',
    'requirements.in',
    'requirements.lock',
    'requirements-build.lock'
)
$tempRoot = Join-Path $stagePath '_payload'
if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
[IO.Directory]::CreateDirectory($tempRoot) | Out-Null
foreach ($relative in $sourceFiles) {
    $src = Join-Path $repoRoot $relative
    $dst = Join-Path $tempRoot $relative
    [IO.Directory]::CreateDirectory((Split-Path -Parent $dst)) | Out-Null
    Copy-Item -LiteralPath $src -Destination $dst
}
Compress-Archive -LiteralPath (Join-Path $tempRoot '*') -DestinationPath $artifactPath -Force
Remove-Item -LiteralPath $tempRoot -Recurse -Force

& (Join-Path $PSScriptRoot 'New-SpdxSbom.ps1') -InputDirectory $stagePath -OutputPath (Join-Path $stagePath 'sbom.spdx.json') -VersionJsonPath (Join-Path $repoRoot 'release\version.json')
Write-Sha256Manifest $stagePath

$evidence = [ordered]@{
    schema_version = 1
    product = $version.product
    version = $version.version
    build_id = $version.build_id
    artifact_name = $artifactName
    signing = [ordered]@{ status = 'BLOCKED'; reason = 'No explicit signing credentials were supplied' }
    timestamp = [ordered]@{ status = 'BLOCKED'; reason = 'No timestamped Authenticode signature is present' }
}
Write-Utf8NoBom (Join-Path $stagePath 'release-evidence.json') ($evidence | ConvertTo-Json -Depth 8)
$evidence
