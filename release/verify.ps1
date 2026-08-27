param(
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

function Get-ReleaseRelativePath {
    param([string]$RelativePath)
    if (-not $RelativePath) { throw 'empty release path' }
    $path = ($RelativePath -replace '\\', '/')
    if ($path -match '^[A-Za-z]:|^/|//') { throw "path traversal or escape in release path: $RelativePath" }
    $parts = @($path -split '/' | Where-Object { $_ -ne '' })
    if ($parts.Count -eq 0) { throw 'empty release path' }
    foreach ($part in $parts) {
        if ($part -eq '.' -or $part -eq '..') { throw "path traversal or escape in release path: $RelativePath" }
    }
    return ($parts -join '/')
}

function Assert-NoForbiddenPath {
    param([string]$RelativePath)
    $normalized = Get-ReleaseRelativePath $RelativePath
    if ($normalized -match '(^|/)(config\.local\.json|cookies?\.txt|master\.db)$') { throw "forbidden staged file: $RelativePath" }
    if ($normalized -match '(?i)(credential|secret|token|cookie|profile|appdata|localappdata|rekordbox|master\.db|\.sqlite|\.db$|\.media$)') {
        throw "forbidden staged file: $RelativePath"
    }
    return $normalized
}

function Get-ActualReleaseFiles {
    param([string]$StagePath)
    $files = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $StagePath -Recurse -Force -File | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($StagePath.Length).TrimStart('\') -replace '\\', '/'
        $relative = Assert-NoForbiddenPath $relative
        if ($relative -ne 'SHA256SUMS.txt') {
            $files += [pscustomobject]@{ RelativePath = $relative; FullName = $file.FullName; Sha256 = Get-Sha256 $file.FullName }
        }
    }
    return $files
}

function Read-ManifestEntries {
    param([string]$StagePath)
    $manifestPath = Join-Path $StagePath 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'SHA-256 manifest is missing' }
    $entries = @()
    $seen = @{}
    foreach ($line in @(Get-Content -LiteralPath $manifestPath)) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "invalid SHA-256 manifest line: $line" }
        $relative = Assert-NoForbiddenPath $Matches[2]
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "duplicate or case-confusable manifest path: $relative" }
        $seen[$key] = $true
        $entries += [pscustomobject]@{ RelativePath = $relative; Sha256 = $Matches[1] }
    }
    return $entries
}

function Assert-ManifestExact {
    param([string]$StagePath)
    $entries = Read-ManifestEntries $StagePath
    $actual = Get-ActualReleaseFiles $StagePath
    $actualByPath = @{}
    foreach ($file in $actual) {
        $key = $file.RelativePath.ToLowerInvariant()
        if ($actualByPath.ContainsKey($key)) { throw "duplicate or case-confusable staged path: $($file.RelativePath)" }
        $actualByPath[$key] = $file
    }
    $manifestByPath = @{}
    foreach ($entry in $entries) { $manifestByPath[$entry.RelativePath.ToLowerInvariant()] = $entry }

    foreach ($entry in $entries) {
        $key = $entry.RelativePath.ToLowerInvariant()
        if (-not $actualByPath.ContainsKey($key)) { throw "manifest target missing: $($entry.RelativePath)" }
        if ($actualByPath[$key].Sha256 -ne $entry.Sha256) { throw "manifest hash mismatch: $($entry.RelativePath)" }
    }
    foreach ($file in $actual) {
        if (-not $manifestByPath.ContainsKey($file.RelativePath.ToLowerInvariant())) {
            throw "extra staged file not listed in manifest: $($file.RelativePath)"
        }
    }
    return $entries
}

function Read-ReleaseEvidence {
    param([string]$StagePath, $ManifestEntries)
    $evidencePath = Join-Path $StagePath 'release-evidence.json'
    if (-not (Test-Path -LiteralPath $evidencePath)) { throw 'release evidence is missing' }
    $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
    foreach ($required in @('schema_version', 'product', 'version', 'build_id', 'source_revision', 'artifacts', 'signing', 'timestamp')) {
        if (-not ($evidence.PSObject.Properties.Name -contains $required)) { throw "malformed release evidence: missing $required" }
    }
    if ($evidence.product -ne 'DeckPipe' -or $evidence.version -ne '0.6.0') { throw 'malformed release evidence: product/version mismatch' }
    if ($evidence.build_id -notmatch '^0\.6\.0\+[0-9]{8}\.[0-9]{6}\.[0-9a-f]{7,40}$') { throw 'malformed release evidence: invalid build_id' }
    if ($evidence.source_revision -notmatch '^[0-9a-f]{40}$') { throw 'malformed release evidence: invalid source revision' }

    $manifestByPath = @{}
    foreach ($entry in $ManifestEntries) { $manifestByPath[$entry.RelativePath.ToLowerInvariant()] = $entry }
    foreach ($artifact in @($evidence.artifacts)) {
        foreach ($required in @('path', 'sha256', 'type')) {
            if (-not ($artifact.PSObject.Properties.Name -contains $required)) { throw "malformed release evidence: artifact missing $required" }
        }
        $relative = Assert-NoForbiddenPath $artifact.path
        if ([IO.Path]::GetExtension($relative) -notin @('.exe', '.msi')) { throw "malformed release evidence: unexpected artifact extension $relative" }
        if ($artifact.type -notin @('exe', 'msi', 'installer')) { throw "malformed release evidence: unexpected artifact type $($artifact.type)" }
        $key = $relative.ToLowerInvariant()
        if (-not $manifestByPath.ContainsKey($key)) { throw "malformed release evidence: artifact missing from manifest $relative" }
        if ($manifestByPath[$key].Sha256 -ne $artifact.sha256) { throw "malformed release evidence: artifact hash mismatch $relative" }
    }
    return $evidence
}

function Assert-SbomExact {
    param([string]$StagePath, $ManifestEntries)
    $sbomPath = Join-Path $StagePath 'sbom.spdx.json'
    if (-not (Test-Path -LiteralPath $sbomPath)) { throw 'SPDX SBOM is missing' }
    $sbom = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
    if ($sbom.spdxVersion -ne 'SPDX-2.3') { throw 'invalid SPDX version' }

    $describes = @($sbom.relationships | Where-Object { $_.spdxElementId -eq 'SPDXRef-DOCUMENT' -and $_.relationshipType -eq 'DESCRIBES' -and $_.relatedSpdxElement -eq 'SPDXRef-Package-DeckPipe' })
    if ($describes.Count -ne 1) { throw 'SPDX SBOM must contain DOCUMENT DESCRIBES Package relationship' }

    $expected = @{}
    foreach ($entry in $ManifestEntries) {
        if ($entry.RelativePath -ne 'sbom.spdx.json') { $expected[$entry.RelativePath.ToLowerInvariant()] = $entry }
    }
    $fileIds = @{}
    foreach ($file in @($sbom.files)) {
        $relative = Assert-NoForbiddenPath $file.fileName
        $key = $relative.ToLowerInvariant()
        if ($fileIds.ContainsKey($file.SPDXID)) { throw "duplicate SPDXID: $($file.SPDXID)" }
        $fileIds[$file.SPDXID] = $true
        if (-not $expected.ContainsKey($key)) { throw "SBOM contains unexpected file: $relative" }
        $shaEntries = @($file.checksums | Where-Object { $_.algorithm -eq 'SHA256' } | Select-Object -First 1)
        if ($shaEntries.Count -ne 1 -or $shaEntries[0].checksumValue -ne $expected[$key].Sha256) { throw "SBOM checksum mismatch: $relative" }
        $contains = @($sbom.relationships | Where-Object { $_.spdxElementId -eq 'SPDXRef-Package-DeckPipe' -and $_.relationshipType -eq 'CONTAINS' -and $_.relatedSpdxElement -eq $file.SPDXID })
        if ($contains.Count -ne 1) { throw "SBOM missing Package CONTAINS File relationship: $relative" }
        $expected.Remove($key)
    }
    if ($expected.Count -gt 0) { throw 'SBOM is missing manifest file entries' }
}

function Get-SignedReleaseArtifacts {
    param([string]$StagePath)
    return @(Get-ChildItem -LiteralPath $StagePath -Recurse -Force -File |
        Where-Object { $_.Extension -in @('.exe', '.msi') } |
        Sort-Object FullName)
}

function Invoke-ReleaseVerification {
    param(
        [string]$StagingDirectory,
        [scriptblock]$SignatureProbe
    )
    if (-not $StagingDirectory) { return New-Result 'BLOCKED' 'Staging directory is missing' }
    $stagePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StagingDirectory)
    if (-not (Test-Path -LiteralPath $stagePath)) { return New-Result 'BLOCKED' 'Staging directory is missing' }
    if (-not (Test-Path -LiteralPath (Join-Path $stagePath 'SHA256SUMS.txt'))) { return New-Result 'BLOCKED' 'SHA-256 manifest is missing' }
    if (-not (Test-Path -LiteralPath (Join-Path $stagePath 'sbom.spdx.json'))) { return New-Result 'BLOCKED' 'SPDX SBOM is missing' }
    if (-not (Test-Path -LiteralPath (Join-Path $stagePath 'release-evidence.json'))) { return New-Result 'BLOCKED' 'Release evidence is missing' }
    if ($null -eq $SignatureProbe) {
        $SignatureProbe = { param($ArtifactPath) Get-AuthenticodeSignature -LiteralPath $ArtifactPath }
    }

    try {
        $manifestEntries = Assert-ManifestExact $stagePath
        $evidence = Read-ReleaseEvidence $stagePath $manifestEntries
        $artifacts = @(Get-SignedReleaseArtifacts $stagePath)
        if ($artifacts.Count -eq 0) { return New-Result 'BLOCKED' 'At least one signed executable or installer artifact is required' }
        if ($evidence.signing.status -ne 'PASS' -or $evidence.timestamp.status -ne 'PASS') {
            return New-Result 'BLOCKED' 'Release evidence does not contain PASS signing and timestamp status'
        }
        foreach ($artifact in $artifacts) {
            $signature = & $SignatureProbe $artifact.FullName
            if ($signature.Status -ne 'Valid') { return New-Result 'BLOCKED' "Authenticode signature is not valid for $($artifact.Name)" }
            if ($null -eq $signature.TimeStamperCertificate) { return New-Result 'BLOCKED' "Authenticode timestamp is missing for $($artifact.Name)" }
        }
        Assert-SbomExact $stagePath $manifestEntries
        return New-Result 'PASS' 'Release evidence, manifest, SBOM, Authenticode signatures, and timestamps are valid'
    } catch {
        return New-Result 'FAIL' $_.Exception.Message
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-ReleaseVerification -StagingDirectory $StagingDirectory
    $result | ConvertTo-Json -Depth 6
    if ($result.status -eq 'FAIL') { exit 1 }
    exit 0
}
