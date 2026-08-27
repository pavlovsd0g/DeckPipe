param(
    [Parameter(Mandatory = $true)]
    [string]$StagingDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function New-Result {
    param([string]$Status, [string]$Message)
    [pscustomobject]@{
        schema_version = 1
        status = $Status
        message = $Message
    }
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

function Test-ForbiddenFiles {
    param([string]$Path)
    $patterns = @('config.local.json', '*.secure.json', '*.sqlite', '*.db', '*.cookie', '*.cookies', '*credential*', '*secret*', '*token*')
    foreach ($pattern in $patterns) {
        $hit = Get-ChildItem -LiteralPath $Path -Recurse -Force -File -Filter $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $hit) { throw "forbidden staged file: $($hit.Name)" }
    }
}

$stagePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StagingDirectory)
if (-not (Test-Path -LiteralPath $stagePath)) {
    New-Result 'BLOCKED' 'Staging directory is missing'
    exit 0
}

try {
    Test-ForbiddenFiles $stagePath
    $manifestPath = Join-Path $stagePath 'SHA256SUMS.txt'
    $sbomPath = Join-Path $stagePath 'sbom.spdx.json'
    $evidencePath = Join-Path $stagePath 'release-evidence.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) { New-Result 'BLOCKED' 'SHA-256 manifest is missing'; exit 0 }
    if (-not (Test-Path -LiteralPath $sbomPath)) { New-Result 'BLOCKED' 'SPDX SBOM is missing'; exit 0 }
    if (-not (Test-Path -LiteralPath $evidencePath)) { New-Result 'BLOCKED' 'Release evidence is missing'; exit 0 }

    $sbom = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
    if ($sbom.spdxVersion -ne 'SPDX-2.3') { throw 'invalid SPDX version' }

    $manifestLines = Get-Content -LiteralPath $manifestPath
    foreach ($line in $manifestLines) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "invalid SHA-256 manifest line: $line" }
        $expected = $Matches[1]
        $relative = $Matches[2] -replace '/', '\'
        $target = Join-Path $stagePath $relative
        if (-not (Test-Path -LiteralPath $target)) { throw "manifest target missing: $relative" }
        if ((Get-Sha256 $target) -ne $expected) { throw "manifest hash mismatch: $relative" }
    }

    $signedArtifacts = Get-ChildItem -LiteralPath $stagePath -File -Include '*.exe','*.msi','*.dll' -ErrorAction SilentlyContinue
    foreach ($artifact in $signedArtifacts) {
        $signature = Get-AuthenticodeSignature -LiteralPath $artifact.FullName
        if ($signature.Status -ne 'Valid') { New-Result 'BLOCKED' "Authenticode signature is not valid for $($artifact.Name)"; exit 0 }
        if ($null -eq $signature.TimeStamperCertificate) { New-Result 'BLOCKED' "Authenticode timestamp is missing for $($artifact.Name)"; exit 0 }
    }

    New-Result 'BLOCKED' 'Signing and timestamp gates are not satisfied by an authorized release artifact'
} catch {
    New-Result 'FAIL' $_.Exception.Message
    exit 1
}
