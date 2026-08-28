[CmdletBinding()]
param(
    [string]$ExePath = '',
    [string]$ExpectedSha256 = '',
    [string]$ExpectedVersion = '',
    [string]$ExpectedBuildId = '',
    [string]$CandidateVersionJsonPath = '',
    [string]$CandidateEvidenceDirectory = '',
    [uri]$BaseUri = $null,
    [string]$OutputDirectory = '',
    [ValidateRange(1, 20)]
    [int]$Samples = 5,
    [ValidateRange(5, 60)]
    [int]$StartupTimeoutSeconds = 30,
    [switch]$NoFailOnFindings,
    [switch]$IsolatedUi,
    [switch]$ValidateOnly,
    [switch]$AllowUnsignedEngineeringEvidence,
    [ValidateSet('', 'identity', 'isolated-preflight', 'listener-owned', 'listener-none', 'listener-multiple', 'listener-unowned', 'listener-wrong-path', 'listener-pid-reuse', 'mandatory-skips', 'verifier-process-contract', 'reparse-attribute-guard')]
    [string]$SelfTestContract = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($null -eq (Get-Command Get-FileHash -ErrorAction SilentlyContinue)) {
    function global:Get-FileHash {
        param(
            [Parameter(Mandatory)][string]$LiteralPath,
            [string]$Algorithm = 'SHA256'
        )

        if ($Algorithm -ne 'SHA256') {
            throw 'Only SHA256 is supported by the DeckPipe QA Get-FileHash fallback.'
        }
        $stream = [IO.File]::OpenRead($LiteralPath)
        try {
            $sha = [Security.Cryptography.SHA256]::Create()
            try {
                $hashBytes = $sha.ComputeHash($stream)
                $builder = [Text.StringBuilder]::new()
                foreach ($byte in $hashBytes) {
                    [void]$builder.Append($byte.ToString('x2'))
                }
                [pscustomobject]@{ Hash = $builder.ToString().ToUpperInvariant() }
            } finally {
                $sha.Dispose()
            }
        } finally {
            $stream.Dispose()
        }
    }
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$modulePath = Join-Path $PSScriptRoot 'DeckPipe.QA.psm1'
$budgetPath = Join-Path $PSScriptRoot 'performance-budget.json'
$releaseVerifierPath = Join-Path $repoRoot 'release\verify.ps1'
$privateBetaPolicyPath = Join-Path $repoRoot 'release\policy.json'
$privateBetaPolicy = [ordered]@{
    schema_version = 1
    channel = 'private-beta'
    signing_requirement = 'owner-waived'
    timestamp_requirement = 'owner-waived'
    windows_reputation_warning = 'accepted'
    waiver_date = '2026-08-28'
    intended_audience = 'controlled-small-group'
}
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot '.bench\runs'
}

Import-Module $modulePath -Force

function New-DeckPipeUnicodeString {
    param([Parameter(Mandatory)][int[]]$CodePoints)

    $builder = [Text.StringBuilder]::new()
    foreach ($codePoint in $CodePoints) {
        if ($codePoint -gt 0xFFFF) {
            [void]$builder.Append([char]::ConvertFromUtf32($codePoint))
        } else {
            [void]$builder.Append([char]$codePoint)
        }
    }
    return $builder.ToString()
}

function Get-PropertyValue {
    param(
        [AllowNull()]$InputObject,
        [Parameter(Mandatory)][string]$Name,
        [AllowNull()]$Default = $null
    )

    if ($null -eq $InputObject) { return $Default }
    if ($InputObject -is [Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) { return $InputObject[$Name] }
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Test-DeckPipeHasReparseAttribute {
    param([Parameter(Mandatory)]$Attributes)

    return [bool](([IO.FileAttributes]$Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
}

function Assert-DeckPipeNoReparseAttributes {
    param(
        [Parameter(Mandatory)]$Attributes,
        [Parameter(Mandatory)][string]$Context
    )

    if (Test-DeckPipeHasReparseAttribute -Attributes $Attributes) {
        throw "$Context reparse point is not allowed."
    }
}

function Assert-DeckPipeItemNotReparse {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Context
    )

    $item = Get-Item -LiteralPath $Path -Force
    Assert-DeckPipeNoReparseAttributes -Attributes $item.Attributes -Context $Context
    return $item
}

function Get-DeckPipeReleaseRelativePath {
    param([Parameter(Mandatory)][string]$RelativePath)

    if ([string]::IsNullOrWhiteSpace($RelativePath)) { throw 'empty release path' }
    $path = ($RelativePath -replace '\\', '/')
    if ($path -match '^[A-Za-z]:|^/|//|:') {
        throw "path traversal, ADS, or escape in release path: $RelativePath"
    }
    $parts = @($path -split '/' | Where-Object { $_ -ne '' })
    if ($parts.Count -eq 0) { throw 'empty release path' }
    foreach ($part in $parts) {
        if ($part -eq '.' -or $part -eq '..') {
            throw "path traversal or escape in release path: $RelativePath"
        }
    }
    if ($parts.Count -ne 1) { throw "nested release paths are not allowed: $RelativePath" }
    return $parts[0]
}

function Assert-DeckPipeNoForbiddenReleasePath {
    param([Parameter(Mandatory)][string]$RelativePath)

    $normalized = Get-DeckPipeReleaseRelativePath -RelativePath $RelativePath
    if ($normalized -match '(^|/)(config\.local\.json|cookies?\.txt|master\.db)$') {
        throw "forbidden staged file: $RelativePath"
    }
    if ($normalized -match '(?i)(credential|secret|token|cookie|profile|appdata|localappdata|rekordbox|master\.db|\.sqlite|\.db$|\.media$)') {
        throw "forbidden staged file: $RelativePath"
    }
    return $normalized
}

function Test-DeckPipeArtifactPath {
    param([Parameter(Mandatory)][string]$RelativePath)

    $extension = [IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
    return ($extension -eq '.exe' -or $extension -eq '.msi')
}

function Get-DeckPipeArtifactType {
    param([Parameter(Mandatory)][string]$RelativePath)

    $extension = [IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
    if ($extension -eq '.exe') { return 'exe' }
    if ($extension -eq '.msi') { return 'msi' }
    throw "unexpected artifact extension: $RelativePath"
}

function Assert-DeckPipeCanonicalArtifactName {
    param(
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)]$Evidence,
        [string]$ArtifactLabel = ''
    )

    $shortRevision = ([string]$Evidence.source_revision).Substring(0, 7)
    $prefix = 'DeckPipe-' + [regex]::Escape([string]$Evidence.build_id) + '-' + [regex]::Escape($shortRevision)
    if (-not [string]::IsNullOrWhiteSpace($ArtifactLabel)) {
        $prefix += '-' + [regex]::Escape($ArtifactLabel)
    }
    $pattern = '^' + $prefix + '-x64(\.exe|-setup\.exe|\.msi)$'
    if ($RelativePath -notmatch $pattern) {
        throw "ambiguous executable artifact or non-canonical artifact name for evidence version/build/source: $RelativePath"
    }
}

function Assert-DeckPipeExactPrivateBetaPolicyObject {
    param(
        [Parameter(Mandatory)]$Policy,
        [Parameter(Mandatory)][string]$Context
    )

    $actualNames = @($Policy.PSObject.Properties.Name)
    foreach ($name in @($privateBetaPolicy.Keys)) {
        if (-not ($actualNames -contains $name)) { throw "$Context private-beta policy missing $name" }
        $value = $Policy.$name
        if ($name -eq 'schema_version') {
            if (-not ($value -is [int] -or $value -is [long]) -or [int64]$value -ne 1) {
                throw "$Context private-beta policy schema_version must be integer 1"
            }
        } elseif (-not ($value -is [string]) -or [string]$value -cne [string]$privateBetaPolicy[$name]) {
            throw "$Context private-beta policy string drift: $name"
        }
    }
    foreach ($name in $actualNames) {
        if (-not $privateBetaPolicy.Contains($name)) { throw "$Context private-beta policy extra $name" }
    }
}

function Get-DeckPipePrivateBetaPolicyRecord {
    if (-not (Test-Path -LiteralPath $privateBetaPolicyPath -PathType Leaf)) {
        throw 'tracked private-beta policy is missing: policy.json'
    }
    $raw = Get-Content -LiteralPath $privateBetaPolicyPath -Raw
    $policy = $raw | ConvertFrom-Json
    Assert-DeckPipeExactPrivateBetaPolicyObject -Policy $policy -Context 'tracked'
    return [pscustomobject]@{
        Raw = $raw
        Sha256 = (Get-FileHash -LiteralPath $privateBetaPolicyPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
}

function Assert-DeckPipeKeySetsEqual {
    param(
        [hashtable]$Expected,
        [hashtable]$Actual,
        [string]$Context
    )

    foreach ($key in @($Expected.Keys)) {
        if (-not $Actual.ContainsKey($key)) { throw "$Context missing: $($Expected[$key])" }
    }
    foreach ($key in @($Actual.Keys)) {
        if (-not $Expected.ContainsKey($key)) { throw "$Context extra: $($Actual[$key])" }
    }
}

function Get-DeckPipeStageFileRecords {
    param([Parameter(Mandatory)][string]$StagePath)

    Assert-DeckPipeItemNotReparse -Path $StagePath -Context 'CandidateEvidenceDirectory' | Out-Null
    $records = @()
    $seen = @{}
    $metadataFiles = @('release-evidence.json', 'sbom.spdx.json', 'SHA256SUMS.txt')
    $optionalMetadataFiles = @('policy.json')
    foreach ($entry in @(Get-ChildItem -LiteralPath $StagePath -Force | Sort-Object Name)) {
        Assert-DeckPipeNoReparseAttributes -Attributes $entry.Attributes -Context "evidence top-level entry $($entry.Name)"
        if ($entry.PSIsContainer) { throw "staging subdirectories are not allowed: $($entry.Name)" }
        $relative = Assert-DeckPipeNoForbiddenReleasePath -RelativePath $entry.Name
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "duplicate or case-confusable staged path: $relative" }
        $seen[$key] = $true

        $metadataMatch = @((@($metadataFiles) + @($optionalMetadataFiles)) | Where-Object { $_ -ieq $relative })
        if ($metadataMatch.Count -gt 0 -and $metadataMatch[0] -cne $relative) {
            throw "case-confusable release metadata path: $relative"
        }
        if ($metadataMatch.Count -eq 0 -and -not (Test-DeckPipeArtifactPath -RelativePath $relative)) {
            throw "extra staged file outside release allowlist: $relative"
        }
        $records += [pscustomobject]@{
            RelativePath = $relative
            FullName = $entry.FullName
            Sha256 = (Get-FileHash -LiteralPath $entry.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        }
    }
    foreach ($required in $metadataFiles) {
        if (-not $seen.ContainsKey($required.ToLowerInvariant())) {
            throw "required release file is missing: $required"
        }
    }
    return $records
}

function Read-DeckPipeManifestEntries {
    param([Parameter(Mandatory)][string]$StagePath)

    $manifestPath = Join-Path $StagePath 'SHA256SUMS.txt'
    Assert-DeckPipeItemNotReparse -Path $manifestPath -Context 'evidence top-level entry SHA256SUMS.txt' | Out-Null
    $entries = @()
    $seen = @{}
    foreach ($line in @(Get-Content -LiteralPath $manifestPath)) {
        if (-not $line.Trim()) { continue }
        if ($line -notmatch '^([0-9a-f]{64})  (.+)$') { throw "invalid SHA-256 manifest line: $line" }
        $relative = Assert-DeckPipeNoForbiddenReleasePath -RelativePath $Matches[2]
        if ($relative -eq 'SHA256SUMS.txt') { throw 'SHA-256 manifest must not contain itself' }
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "duplicate or case-confusable manifest path: $relative" }
        $seen[$key] = $true
        $entries += [pscustomobject]@{ RelativePath = $relative; Sha256 = $Matches[1] }
    }
    return $entries
}

function Assert-DeckPipeManifestExact {
    param(
        [Parameter(Mandatory)][string]$StagePath,
        [object[]]$ActualFiles = $null
    )

    if ($null -eq $ActualFiles) { $ActualFiles = @(Get-DeckPipeStageFileRecords -StagePath $StagePath) }
    $entries = @(Read-DeckPipeManifestEntries -StagePath $StagePath)
    $actualByPath = @{}
    foreach ($file in @($ActualFiles | Where-Object { $_.RelativePath -ne 'SHA256SUMS.txt' })) {
        $actualByPath[$file.RelativePath.ToLowerInvariant()] = $file.RelativePath
    }
    $manifestByPath = @{}
    foreach ($entry in $entries) { $manifestByPath[$entry.RelativePath.ToLowerInvariant()] = $entry.RelativePath }
    Assert-DeckPipeKeySetsEqual -Expected $actualByPath -Actual $manifestByPath -Context 'manifest/staging inventory mismatch'

    $actualHashByPath = @{}
    foreach ($file in $ActualFiles) { $actualHashByPath[$file.RelativePath.ToLowerInvariant()] = $file.Sha256 }
    foreach ($entry in $entries) {
        if ($actualHashByPath[$entry.RelativePath.ToLowerInvariant()] -ne $entry.Sha256) {
            throw "manifest hash mismatch: $($entry.RelativePath)"
        }
    }
    return $entries
}

function Read-DeckPipeReleaseEvidence {
    param(
        [Parameter(Mandatory)][string]$StagePath,
        [Parameter(Mandatory)]$ManifestEntries
    )

    $evidencePath = Join-Path $StagePath 'release-evidence.json'
    Assert-DeckPipeItemNotReparse -Path $evidencePath -Context 'evidence top-level entry release-evidence.json' | Out-Null
    $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
    foreach ($required in @('schema_version', 'product', 'version', 'build_id', 'source_revision', 'artifacts', 'signing', 'timestamp')) {
        if (-not ($evidence.PSObject.Properties.Name -contains $required)) {
            throw "malformed release evidence: missing $required"
        }
    }
    foreach ($required in @('status', 'signed')) {
        if (-not ($evidence.signing.PSObject.Properties.Name -contains $required)) {
            throw "malformed release evidence: signing missing $required"
        }
    }
    if (-not ($evidence.timestamp.PSObject.Properties.Name -contains 'status')) {
        throw 'malformed release evidence: timestamp missing status'
    }
    if ($evidence.product -ne 'DeckPipe' -or $evidence.version -ne '0.6.0') {
        throw 'malformed release evidence: product/version mismatch'
    }
    if ($evidence.build_id -notmatch '^0\.6\.0\+[0-9]{8}\.[0-9]{6}\.[0-9a-f]{7,40}$') {
        throw 'malformed release evidence: invalid build_id'
    }
    if ($evidence.source_revision -notmatch '^[0-9a-f]{40}$') {
        throw 'malformed release evidence: invalid source revision'
    }

    $manifestByPath = @{}
    foreach ($entry in $ManifestEntries) { $manifestByPath[$entry.RelativePath.ToLowerInvariant()] = $entry }
    $hasDistribution = $evidence.PSObject.Properties.Name -contains 'distribution'
    $signingStatus = [string]$evidence.signing.status
    $timestampStatus = [string]$evidence.timestamp.status
    $hasSigningPolicyPath = $evidence.signing.PSObject.Properties.Name -contains 'policy_path'
    $hasTimestampPolicyPath = $evidence.timestamp.PSObject.Properties.Name -contains 'policy_path'
    $isWaived = $signingStatus -eq 'WAIVED_BY_OWNER' -or $timestampStatus -eq 'WAIVED_BY_OWNER' -or $hasSigningPolicyPath -or $hasTimestampPolicyPath
    $isPrivateBeta = $false
    $artifactLabel = ''

    if ($hasDistribution -or $isWaived) {
        if (-not $hasDistribution) { throw 'WAIVED_BY_OWNER requires exact private-beta policy distribution evidence' }
        foreach ($required in @('channel', 'artifact_label', 'policy_path', 'policy_sha256')) {
            if (-not ($evidence.distribution.PSObject.Properties.Name -contains $required)) {
                throw "private-beta distribution missing $required"
            }
        }
        if ([string]$evidence.distribution.channel -ne 'private-beta') {
            throw "WAIVED_BY_OWNER requires private-beta distribution channel, got $($evidence.distribution.channel)"
        }
        if ([string]$evidence.distribution.artifact_label -ne 'unsigned-private-beta') {
            throw 'private-beta distribution artifact_label must be unsigned-private-beta'
        }
        if ([string]$evidence.distribution.policy_path -cne 'policy.json') {
            throw 'private-beta distribution policy_path must be policy.json, not a caller-supplied path'
        }
        if ($signingStatus -ne 'WAIVED_BY_OWNER' -or $timestampStatus -ne 'WAIVED_BY_OWNER') {
            throw 'private-beta policy evidence requires WAIVED_BY_OWNER signing and timestamp status'
        }
        if (-not $hasSigningPolicyPath -or [string]$evidence.signing.policy_path -cne 'policy.json') {
            throw 'private-beta signing policy_path must be policy.json'
        }
        if (-not $hasTimestampPolicyPath -or [string]$evidence.timestamp.policy_path -cne 'policy.json') {
            throw 'private-beta timestamp policy_path must be policy.json'
        }
        if (@($evidence.signing.signed).Count -ne 0) {
            throw 'private-beta WAIVED_BY_OWNER evidence requires empty signing.signed'
        }
        if ([string]$evidence.distribution.policy_sha256 -cnotmatch '^[0-9a-f]{64}$') {
            throw 'private-beta policy_sha256 must be a lowercase SHA-256 digest'
        }
        if (-not $manifestByPath.ContainsKey('policy.json')) { throw 'private-beta policy missing from manifest' }
        $trackedPolicy = Get-DeckPipePrivateBetaPolicyRecord
        $stagedPolicyPath = Join-Path $StagePath 'policy.json'
        Assert-DeckPipeItemNotReparse -Path $stagedPolicyPath -Context 'evidence top-level entry policy.json' | Out-Null
        $stagedPolicyHash = (Get-FileHash -LiteralPath $stagedPolicyPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($stagedPolicyHash -cne $trackedPolicy.Sha256) {
            throw 'staged private-beta policy sha256 must match tracked policy.json byte-for-byte'
        }
        $stagedPolicy = Get-Content -LiteralPath $stagedPolicyPath -Raw | ConvertFrom-Json
        Assert-DeckPipeExactPrivateBetaPolicyObject -Policy $stagedPolicy -Context 'staged'
        if ($stagedPolicyHash -cne [string]$evidence.distribution.policy_sha256) {
            throw 'private-beta policy_sha256 hash mismatch'
        }
        if ($manifestByPath['policy.json'].Sha256 -cne $stagedPolicyHash) {
            throw 'private-beta manifest policy hash mismatch'
        }
        $isPrivateBeta = $true
        $artifactLabel = 'unsigned-private-beta'
    }

    $artifacts = @($evidence.artifacts)
    $signed = @($evidence.signing.signed)
    if ($artifacts.Count -eq 0) { throw 'release evidence artifacts must be nonempty' }
    if (-not $isPrivateBeta -and $signed.Count -eq 0) { throw 'release evidence signing.signed must be nonempty' }

    $artifactByPath = @{}
    foreach ($artifact in $artifacts) {
        foreach ($required in @('path', 'sha256', 'type')) {
            if (-not ($artifact.PSObject.Properties.Name -contains $required)) {
                throw "malformed release evidence: artifact missing $required"
            }
        }
        $relative = Assert-DeckPipeNoForbiddenReleasePath -RelativePath ([string]$artifact.path)
        Assert-DeckPipeCanonicalArtifactName -RelativePath $relative -Evidence $evidence -ArtifactLabel $artifactLabel
        $expectedType = Get-DeckPipeArtifactType -RelativePath $relative
        if ([string]$artifact.type -ne $expectedType) {
            throw "malformed release evidence: artifact type/extension mismatch $relative"
        }
        $key = $relative.ToLowerInvariant()
        if ($artifactByPath.ContainsKey($key)) { throw "duplicate or case-confusable evidence artifact path: $relative" }
        $artifactByPath[$key] = $relative
        if (-not $manifestByPath.ContainsKey($key)) {
            throw "malformed release evidence: artifact missing from manifest $relative"
        }
        if ($manifestByPath[$key].Sha256 -ne [string]$artifact.sha256) {
            throw "malformed release evidence: artifact hash mismatch $relative"
        }
    }

    $signedByPath = @{}
    foreach ($signedPath in $signed) {
        $relative = Assert-DeckPipeNoForbiddenReleasePath -RelativePath ([string]$signedPath)
        $key = $relative.ToLowerInvariant()
        if ($signedByPath.ContainsKey($key)) { throw "duplicate or case-confusable signed artifact path: $relative" }
        $signedByPath[$key] = $relative
    }
    if (-not $isPrivateBeta) {
        Assert-DeckPipeKeySetsEqual -Expected $artifactByPath -Actual $signedByPath -Context 'signed/artifact evidence mismatch'
    }
    return $evidence
}

function Assert-DeckPipeSbomExact {
    param(
        [Parameter(Mandatory)][string]$StagePath,
        [Parameter(Mandatory)]$ManifestEntries,
        [Parameter(Mandatory)]$Evidence
    )

    $sbomPath = Join-Path $StagePath 'sbom.spdx.json'
    Assert-DeckPipeItemNotReparse -Path $sbomPath -Context 'evidence top-level entry sbom.spdx.json' | Out-Null
    $sbom = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
    if ($sbom.spdxVersion -ne 'SPDX-2.3') { throw 'invalid SPDX version' }
    $describes = @($sbom.relationships | Where-Object {
        $_.spdxElementId -eq 'SPDXRef-DOCUMENT' -and
        $_.relationshipType -eq 'DESCRIBES' -and
        $_.relatedSpdxElement -eq 'SPDXRef-Package-DeckPipe'
    })
    if ($describes.Count -ne 1) { throw 'SPDX SBOM must contain DOCUMENT DESCRIBES Package relationship' }

    $expected = @{}
    foreach ($entry in $ManifestEntries) {
        if ($entry.RelativePath -ne 'sbom.spdx.json') { $expected[$entry.RelativePath.ToLowerInvariant()] = $entry }
    }
    if (-not $expected.ContainsKey('release-evidence.json')) { throw 'SBOM expected set must include release evidence' }

    $sbomByPath = @{}
    $sbomArtifactByPath = @{}
    $fileIds = @{}
    foreach ($file in @($sbom.files)) {
        $relative = Assert-DeckPipeNoForbiddenReleasePath -RelativePath ([string]$file.fileName)
        $key = $relative.ToLowerInvariant()
        if ($fileIds.ContainsKey($file.SPDXID)) { throw "duplicate SPDXID: $($file.SPDXID)" }
        if ($sbomByPath.ContainsKey($key)) { throw "duplicate or case-confusable SBOM file path: $relative" }
        $fileIds[$file.SPDXID] = $true
        $sbomByPath[$key] = $relative
        if (-not $expected.ContainsKey($key)) { throw "SBOM contains unexpected file: $relative" }
        $shaEntries = @($file.checksums | Where-Object { $_.algorithm -eq 'SHA256' })
        if ($shaEntries.Count -ne 1 -or $shaEntries[0].checksumValue -ne $expected[$key].Sha256) {
            throw "SBOM checksum mismatch: $relative"
        }
        $contains = @($sbom.relationships | Where-Object {
            $_.spdxElementId -eq 'SPDXRef-Package-DeckPipe' -and
            $_.relationshipType -eq 'CONTAINS' -and
            $_.relatedSpdxElement -eq $file.SPDXID
        })
        if ($contains.Count -ne 1) { throw "SBOM missing Package CONTAINS File relationship: $relative" }
        if (Test-DeckPipeArtifactPath -RelativePath $relative) { $sbomArtifactByPath[$key] = $relative }
    }

    $expectedByPath = @{}
    foreach ($key in @($expected.Keys)) { $expectedByPath[$key] = $expected[$key].RelativePath }
    Assert-DeckPipeKeySetsEqual -Expected $expectedByPath -Actual $sbomByPath -Context 'SBOM/manifest file inventory mismatch'

    $evidenceArtifactByPath = @{}
    foreach ($artifact in @($Evidence.artifacts)) {
        $relative = Assert-DeckPipeNoForbiddenReleasePath -RelativePath ([string]$artifact.path)
        $evidenceArtifactByPath[$relative.ToLowerInvariant()] = $relative
    }
    Assert-DeckPipeKeySetsEqual -Expected $evidenceArtifactByPath -Actual $sbomArtifactByPath -Context 'SBOM/evidence artifact mismatch'
}

function ConvertTo-DeckPipeWindowsArgument {
    param([AllowNull()][string]$Argument)

    if ($null -eq $Argument) { $Argument = '' }
    if ($Argument.Length -gt 0 -and $Argument -notmatch '[\s"]') { return $Argument }
    $builder = [Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($char in $Argument.ToCharArray()) {
        if ($char -eq '\') {
            $backslashes++
            continue
        }
        if ($char -eq '"') {
            [void]$builder.Append('\', ($backslashes * 2) + 1)
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append('\', $backslashes)
            $backslashes = 0
        }
        [void]$builder.Append($char)
    }
    if ($backslashes -gt 0) { [void]$builder.Append('\', $backslashes * 2) }
    [void]$builder.Append('"')
    return $builder.ToString()
}

function Resolve-DeckPipeVerifierPowerShell {
    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($PSHOME)) {
        $candidates += (Join-Path $PSHOME 'powershell.exe')
    }
    if (-not [string]::IsNullOrWhiteSpace($env:SystemRoot)) {
        $candidates += (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe')
    }

    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $item = Assert-DeckPipeItemNotReparse -Path $candidate -Context 'release verifier PowerShell executable'
            if ($item.Name -cne 'powershell.exe') {
                throw "release verifier executable is not Windows PowerShell: $candidate"
            }
            return $item.FullName
        }
    }
    throw 'Windows PowerShell executable was not found for release verifier isolation.'
}

function ConvertFrom-DeckPipeVerifierStdout {
    param([AllowNull()][string]$Stdout)

    $trimmed = if ($null -eq $Stdout) { '' } else { $Stdout.Trim() }
    if ([string]::IsNullOrWhiteSpace($trimmed)) {
        throw 'release verifier produced empty stdout.'
    }
    if (-not ($trimmed.StartsWith('{') -and $trimmed.EndsWith('}'))) {
        throw 'release verifier produced noisy stdout instead of exactly one JSON object.'
    }
    try {
        $json = $trimmed | ConvertFrom-Json
    } catch {
        throw "release verifier produced multiple or malformed JSON objects: $($_.Exception.Message)"
    }
    if ($json -is [array]) { throw 'release verifier produced a JSON array instead of one object.' }
    foreach ($required in @('schema_version', 'status', 'message')) {
        if (-not ($json.PSObject.Properties.Name -contains $required)) {
            throw "release verifier JSON missing $required."
        }
    }
    if ([string]$json.status -notin @('PASS', 'FAIL', 'BLOCKED')) {
        throw "release verifier returned unexpected status: $($json.status)"
    }
    return $json
}

function Invoke-DeckPipeReleaseVerifierProcess {
    param(
        [Parameter(Mandatory)][string]$StagePath,
        [string]$VerifierPath = $releaseVerifierPath,
        [int]$TimeoutSeconds = 30
    )

    $verifierItem = Assert-DeckPipeItemNotReparse -Path $VerifierPath -Context 'release verifier script'
    $powerShellExe = Resolve-DeckPipeVerifierPowerShell
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $powerShellExe
    $arguments = @(
        '-NoProfile',
        '-NonInteractive',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $verifierItem.FullName,
        '-StagingDirectory',
        $StagePath
    )
    $startInfo.Arguments = ((@($arguments) | ForEach-Object { ConvertTo-DeckPipeWindowsArgument -Argument ([string]$_) }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    if (-not [string]::IsNullOrWhiteSpace($env:SystemRoot)) {
        $systemModulePath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\Modules'
        if (Test-Path -LiteralPath $systemModulePath -PathType Container) {
            $startInfo.EnvironmentVariables['PSModulePath'] = $systemModulePath
        }
    }

    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    $started = $process.Start()
    if (-not $started) { throw 'release verifier process failed to start.' }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $timeoutMs = [math]::Max(1, $TimeoutSeconds) * 1000
    if (-not $process.WaitForExit($timeoutMs)) {
        try { $process.Kill() } catch {}
        try { $process.WaitForExit(5000) | Out-Null } catch {}
        throw "release verifier timed out after $TimeoutSeconds seconds."
    }
    $process.WaitForExit()
    $stdoutTask.Wait(5000) | Out-Null
    $stderrTask.Wait(5000) | Out-Null
    $stdout = [string]$stdoutTask.Result
    $stderr = [string]$stderrTask.Result
    $json = ConvertFrom-DeckPipeVerifierStdout -Stdout $stdout
    $exitCode = [int]$process.ExitCode
    if (-not [string]::IsNullOrWhiteSpace($stderr)) {
        throw "release verifier wrote stderr while returning $($json.status): $stderr"
    }
    $expectedExit = @{ PASS = 0; FAIL = 1; BLOCKED = 2 }[[string]$json.status]
    if ($exitCode -ne $expectedExit) {
        throw "release verifier status/exit mismatch: status $($json.status) exit $exitCode expected $expectedExit"
    }
    [pscustomobject]@{
        schema_version = [int]$json.schema_version
        status = [string]$json.status
        message = [string]$json.message
        exit_code = $exitCode
        stdout = $stdout
        stderr = $stderr
    }
}

function Resolve-DeckPipeEvidenceDirectory {
    if ([string]::IsNullOrWhiteSpace($CandidateEvidenceDirectory)) {
        throw 'CandidateEvidenceDirectory is required before launching installed QA.'
    }
    if (-not [string]::IsNullOrWhiteSpace($ExpectedSha256) -or
        -not [string]::IsNullOrWhiteSpace($ExpectedVersion) -or
        -not [string]::IsNullOrWhiteSpace($ExpectedBuildId) -or
        -not [string]::IsNullOrWhiteSpace($CandidateVersionJsonPath)) {
        throw 'caller-supplied ExpectedSha256 ExpectedVersion ExpectedBuildId and CandidateVersionJsonPath are deprecated; use CandidateEvidenceDirectory.'
    }
    $stagePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($CandidateEvidenceDirectory)
    if (-not (Test-Path -LiteralPath $stagePath -PathType Container)) {
        throw "CandidateEvidenceDirectory was not found: $CandidateEvidenceDirectory"
    }
    Assert-DeckPipeItemNotReparse -Path $stagePath -Context 'CandidateEvidenceDirectory' | Out-Null
    foreach ($required in @('release-evidence.json', 'SHA256SUMS.txt', 'sbom.spdx.json')) {
        $requiredPath = Join-Path $stagePath $required
        if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
            throw "Candidate evidence is missing $required"
        }
        Assert-DeckPipeItemNotReparse -Path $requiredPath -Context "evidence top-level entry $required" | Out-Null
    }
    Get-DeckPipeStageFileRecords -StagePath $stagePath | Out-Null
    return $stagePath
}

function Read-DeckPipeVerifiedEvidence {
    param([Parameter(Mandatory)][string]$StagePath)

    if (-not (Test-Path -LiteralPath $releaseVerifierPath -PathType Leaf)) {
        throw "Release verifier was not found: $releaseVerifierPath"
    }
    $verification = Invoke-DeckPipeReleaseVerifierProcess -StagePath $StagePath
    $actualFiles = @(Get-DeckPipeStageFileRecords -StagePath $StagePath)
    if ($verification.status -eq 'PASS') {
        $manifestEntries = Assert-DeckPipeManifestExact -StagePath $StagePath -ActualFiles $actualFiles
        $evidence = Read-DeckPipeReleaseEvidence -StagePath $StagePath -ManifestEntries $manifestEntries
        Assert-DeckPipeSbomExact -StagePath $StagePath -ManifestEntries $manifestEntries -Evidence $evidence
        return [pscustomobject]@{
            Verification = $verification
            ManifestEntries = $manifestEntries
            Evidence = $evidence
            LaunchAllowed = $true
            EngineeringMode = $false
        }
    }

    if (-not $AllowUnsignedEngineeringEvidence) {
        throw "release verifier status $($verification.status): $($verification.message)"
    }
    if ($verification.status -ne 'BLOCKED') {
        throw "release verifier status $($verification.status): $($verification.message)"
    }

    $manifestEntries = Assert-DeckPipeManifestExact -StagePath $StagePath -ActualFiles $actualFiles
    $evidence = Read-DeckPipeReleaseEvidence -StagePath $StagePath -ManifestEntries $manifestEntries
    Assert-DeckPipeSbomExact -StagePath $StagePath -ManifestEntries $manifestEntries -Evidence $evidence
    if ($evidence.signing.status -ne 'BLOCKED' -or $evidence.timestamp.status -ne 'BLOCKED') {
        throw 'unsigned engineering evidence requires BLOCKED signing and timestamp status.'
    }
    return [pscustomobject]@{
        Verification = $verification
        ManifestEntries = $manifestEntries
        Evidence = $evidence
        LaunchAllowed = $false
        EngineeringMode = $true
    }
}

function Resolve-DeckPipeStagedArtifactPath {
    param(
        [Parameter(Mandatory)][string]$StagePath,
        [Parameter(Mandatory)][string]$RelativePath
    )

    $combined = Join-Path $StagePath ($RelativePath -replace '/', '\')
    $full = [IO.Path]::GetFullPath($combined)
    $root = [IO.Path]::GetFullPath($StagePath).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar)
    $rootWithSeparator = $root + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)) {
        throw "candidate artifact path escapes evidence directory: $RelativePath"
    }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        throw "candidate artifact file is missing: $RelativePath"
    }
    Assert-DeckPipeItemNotReparse -Path $full -Context "staged artifact $RelativePath" | Out-Null
    return $full
}

function Test-DeckPipeApplicationArtifactPath {
    param(
        [Parameter(Mandatory)]$Evidence,
        [Parameter(Mandatory)][string]$RelativePath
    )

    $shortSource = ([string]$Evidence.source_revision).Substring(0, 7)
    $label = ''
    if ($Evidence.PSObject.Properties.Name -contains 'distribution') {
        $label = '-' + [string]$Evidence.distribution.artifact_label
    }
    $expected = "DeckPipe-$($Evidence.build_id)-$shortSource$label-x64.exe"
    return [string]$RelativePath -ceq $expected
}

function Compare-DeckPipeTauriBundleMarkerPayload {
    param(
        [Parameter(Mandatory)][string]$InstalledPath,
        [Parameter(Mandatory)][string]$StagedPath
    )

    $stagedBytes = [IO.File]::ReadAllBytes($StagedPath)
    $installedBytes = [IO.File]::ReadAllBytes($InstalledPath)
    if ($stagedBytes.Length -ne $installedBytes.Length) {
        return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = 'installed and staged executable lengths differ' }
    }

    $differenceOffsets = [Collections.Generic.List[int]]::new()
    for ($index = 0; $index -lt $stagedBytes.Length; $index++) {
        if ($stagedBytes[$index] -ne $installedBytes[$index]) {
            $differenceOffsets.Add($index)
            if ($differenceOffsets.Count -gt 3) {
                return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = "payload has more than three changed bytes; additional drift at offset $index" }
            }
        }
    }

    if ($differenceOffsets.Count -ne 3) {
        return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = "payload changed byte count is $($differenceOffsets.Count), expected 3" }
    }
    if ($differenceOffsets[1] -ne ($differenceOffsets[0] + 1) -or $differenceOffsets[2] -ne ($differenceOffsets[0] + 2)) {
        return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = 'payload changed bytes are not one contiguous three-byte range' }
    }

    $encoding = [Text.Encoding]::ASCII
    $prefix = $encoding.GetBytes('__TAURI_BUNDLE_TYPE_VAR_')
    $bundleCodeOffset = [int]$differenceOffsets[0]
    $prefixOffset = $bundleCodeOffset - $prefix.Length
    if ($prefixOffset -lt 0) {
        return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = 'changed range is not immediately after the Tauri bundle marker prefix' }
    }
    for ($index = 0; $index -lt $prefix.Length; $index++) {
        if ($stagedBytes[$prefixOffset + $index] -ne $prefix[$index] -or $installedBytes[$prefixOffset + $index] -ne $prefix[$index]) {
            return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = 'changed range is not immediately after the Tauri bundle marker prefix' }
        }
    }

    $stagedBundleType = $encoding.GetString($stagedBytes, $bundleCodeOffset, 3)
    $installedBundleType = $encoding.GetString($installedBytes, $bundleCodeOffset, 3)
    if ($stagedBundleType -cne 'UNK') {
        return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = "staged bundle marker code is invalid: $stagedBundleType" }
    }
    if ($installedBundleType -cnotin @('NSS', 'MSI')) {
        return [pscustomobject]@{ Matched = $false; BundleType = ''; Reason = "installed bundle marker code is invalid: $installedBundleType" }
    }

    return [pscustomobject]@{ Matched = $true; BundleType = $installedBundleType; Reason = 'recognized Tauri bundle marker transform' }
}

function Assert-DeckPipeCandidateIdentity {
    param([Parameter(Mandatory)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw 'ExePath is required before launching installed QA.'
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "DeckPipe executable was not found: $Path"
    }

    $resolved = (Resolve-Path -LiteralPath $Path).Path
    Assert-DeckPipeItemNotReparse -Path $resolved -Context 'installed ExePath' | Out-Null
    $installDirectoryPath = Split-Path -Parent $resolved
    Assert-DeckPipeItemNotReparse -Path $installDirectoryPath -Context 'install directory' | Out-Null
    $stagePath = Resolve-DeckPipeEvidenceDirectory
    $verified = Read-DeckPipeVerifiedEvidence -StagePath $stagePath
    $evidence = $verified.Evidence
    $actualSha = (Get-FileHash -LiteralPath $resolved -Algorithm SHA256).Hash.ToLowerInvariant()
    $artifactMatches = [Collections.Generic.List[object]]::new()
    $markerComparisonReasons = [Collections.Generic.List[string]]::new()
    foreach ($artifact in @($evidence.artifacts)) {
        if ([string]$artifact.type -ne 'exe') { continue }
        $isApplicationArtifact = Test-DeckPipeApplicationArtifactPath -Evidence $evidence -RelativePath ([string]$artifact.path)
        $stagedPath = Resolve-DeckPipeStagedArtifactPath -StagePath $stagePath -RelativePath ([string]$artifact.path)
        $stagedSha = (Get-FileHash -LiteralPath $stagedPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($stagedSha -cne [string]$artifact.sha256) {
            throw "candidate staged artifact hash mismatch: $($artifact.path)"
        }
        if ([string]$artifact.sha256 -cne $actualSha) {
            if (-not $isApplicationArtifact) { continue }
            $markerMatch = Compare-DeckPipeTauriBundleMarkerPayload -InstalledPath $resolved -StagedPath $stagedPath
            if (-not $markerMatch.Matched) {
                $markerComparisonReasons.Add([string]$markerMatch.Reason)
                continue
            }
            $artifactMatches.Add([pscustomobject]@{
                Artifact = $artifact
                StagedPath = $stagedPath
                StagedSha256 = $stagedSha
                IdentityMatchMode = 'tauri-bundle-marker'
                AcceptedBundleType = [string]$markerMatch.BundleType
                IsApplicationArtifact = $true
            })
            continue
        }
        $artifactMatches.Add([pscustomobject]@{
            Artifact = $artifact
            StagedPath = $stagedPath
            StagedSha256 = $stagedSha
            IdentityMatchMode = 'exact-sha256'
            AcceptedBundleType = ''
            IsApplicationArtifact = [bool]$isApplicationArtifact
        })
    }
    if ($artifactMatches.Count -eq 0) {
        $detail = ''
        if ($markerComparisonReasons.Count -gt 0) {
            $detail = ' Last Tauri bundle marker comparison: ' + [string]$markerComparisonReasons[$markerComparisonReasons.Count - 1]
        }
        throw "installed ExePath bytes do not match exactly one application executable artifact in release evidence by exact SHA-256 or recognized Tauri bundle marker.$detail"
    }
    if ($artifactMatches.Count -gt 1) {
        throw 'ambiguous executable artifact: installed ExePath bytes match multiple staged executables.'
    }
    if ([string]$evidence.version -ne [string]$budget.target_version) {
        throw "candidate evidence version mismatch: expected $($budget.target_version) actual $($evidence.version)"
    }

    $match = $artifactMatches[0]
    if (-not [bool]$match.IsApplicationArtifact) {
        throw 'installed ExePath matched a setup artifact, not the application executable artifact.'
    }
    [pscustomobject]@{
        matched = $true
        resolved_exe = $resolved
        install_directory = $installDirectoryPath
        actual_sha256 = $actualSha
        staged_sha256 = [string]$match.StagedSha256
        identity_match_mode = [string]$match.IdentityMatchMode
        accepted_bundle_type = [string]$match.AcceptedBundleType
        actual_version = [string]$evidence.version
        actual_build_id = [string]$evidence.build_id
        source_revision = [string]$evidence.source_revision
        evidence_directory = $stagePath
        staged_artifact = [string]$match.StagedPath
        staged_artifact_path = [string]$match.Artifact.path
        release_verifier_status = [string]$verified.Verification.status
        release_verifier_message = [string]$verified.Verification.message
        engineering_mode = [bool]$verified.EngineeringMode
        launch_allowed = [bool]$verified.LaunchAllowed
    }
}

function Get-DeckPipeProcessInventory {
    @(Get-CimInstance -ClassName Win32_Process | Select-Object ProcessId, ParentProcessId, Name, ExecutablePath)
}

function Get-DeckPipePortOwner {
    param([Parameter(Mandatory)][int]$Port)

    $connection = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $connection) { return $null }
    return [int]$connection.OwningProcess
}

function Test-DeckPipeLoopbackAddress {
    param([AllowNull()][string]$Address)

    if ([string]::IsNullOrWhiteSpace($Address)) { return $false }
    if ($Address -in @('::1', '127.0.0.1', 'localhost')) { return $true }
    return [bool]($Address -match '^127\.')
}

function Get-DeckPipeLoopbackListeners {
    @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { Test-DeckPipeLoopbackAddress -Address ([string]$_.LocalAddress) } |
        Select-Object @{ Name = 'LocalAddress'; Expression = { [string]$_.LocalAddress } },
            @{ Name = 'LocalPort'; Expression = { [int]$_.LocalPort } },
            @{ Name = 'OwningProcess'; Expression = { [int]$_.OwningProcess } })
}

function Test-DeckPipeOwnedProcessChain {
    param(
        [Parameter(Mandatory)][int]$RootProcessId,
        [Parameter(Mandatory)][int]$OwnerProcessId,
        [Parameter(Mandatory)][object[]]$Processes,
        [Parameter(Mandatory)][string]$InstallDirectory
    )

    $byId = @{}
    foreach ($process in @($Processes)) {
        $byId[[int]$process.ProcessId] = $process
    }
    $seen = [Collections.Generic.HashSet[int]]::new()
    $current = $OwnerProcessId
    while ($true) {
        if (-not $byId.ContainsKey($current)) { return $false }
        if (-not $seen.Add($current)) { return $false }
        $process = $byId[$current]
        if (-not (Test-DeckPipeProcessPathOwned -ProcessPath $process.ExecutablePath -InstallDirectory $InstallDirectory)) {
            return $false
        }
        if ($current -eq $RootProcessId) { return $true }
        $current = [int]$process.ParentProcessId
    }
}

function Select-DeckPipeOwnedLoopbackListener {
    param(
        [Parameter(Mandatory)][int]$RootProcessId,
        [Parameter(Mandatory)][object[]]$Processes,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [object[]]$Listeners,
        [Parameter(Mandatory)][string]$InstallDirectory
    )

    $ownedProcessIds = @(Get-DeckPipeDescendantProcessIds -RootProcessId $RootProcessId -Processes $Processes)
    $owned = @($Listeners | Where-Object {
        [int]$_.OwningProcess -in $ownedProcessIds -and
        (Test-DeckPipeLoopbackAddress -Address ([string]$_.LocalAddress)) -and
        (Test-DeckPipeOwnedProcessChain `
            -RootProcessId $RootProcessId `
            -OwnerProcessId ([int]$_.OwningProcess) `
            -Processes $Processes `
            -InstallDirectory $InstallDirectory)
    })
    if ($owned.Count -eq 0) {
        throw 'owned loopback listener was not found for the launched DeckPipe process tree.'
    }
    if ($owned.Count -gt 1) {
        throw 'multiple owned loopback listeners were found for the launched DeckPipe process tree.'
    }
    return $owned[0]
}

function Assert-DeckPipeLoopbackListenerStable {
    param(
        [Parameter(Mandatory)][int]$RootProcessId,
        [Parameter(Mandatory)]$InitialListener,
        [Parameter(Mandatory)][string]$InstallDirectory,
        [object[]]$Processes = $null,
        [object[]]$Listeners = $null
    )

    if ($null -eq $Processes) { $Processes = Get-DeckPipeProcessInventory }
    if ($null -eq $Listeners) { $Listeners = Get-DeckPipeLoopbackListeners }
    try {
        $current = Select-DeckPipeOwnedLoopbackListener `
            -RootProcessId $RootProcessId `
            -Processes $Processes `
            -Listeners $Listeners `
            -InstallDirectory $InstallDirectory
    } catch {
        throw 'listener ownership changed before accept.'
    }
    if ([string]$current.LocalAddress -cne [string]$InitialListener.LocalAddress -or
        [int]$current.LocalPort -ne [int]$InitialListener.LocalPort -or
        [int]$current.OwningProcess -ne [int]$InitialListener.OwningProcess) {
        throw 'listener ownership changed before accept.'
    }
    return $current
}

function Wait-DeckPipeOwnedLoopbackListener {
    param(
        [Parameter(Mandatory)][int]$RootProcessId,
        [Parameter(Mandatory)][int]$TimeoutSeconds,
        [Parameter(Mandatory)][string]$InstallDirectory
    )

    $watch = [Diagnostics.Stopwatch]::StartNew()
    $lastError = $null
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $client = $null
        try {
            $inventory = Get-DeckPipeProcessInventory
            $listener = Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId $RootProcessId `
                -Processes $inventory `
                -Listeners @(Get-DeckPipeLoopbackListeners) `
                -InstallDirectory $InstallDirectory
            $listener = Assert-DeckPipeLoopbackListenerStable `
                -RootProcessId $RootProcessId `
                -InitialListener $listener `
                -InstallDirectory $InstallDirectory
            $hostName = if ([string]$listener.LocalAddress -eq '::1') { '::1' } else { '127.0.0.1' }
            $client = [Net.Sockets.TcpClient]::new()
            $pending = $client.ConnectAsync($hostName, [int]$listener.LocalPort)
            if ($pending.Wait(250) -and $client.Connected) {
                return [pscustomobject]@{
                    BaseUri = [uri]::new("http://127.0.0.1:$([int]$listener.LocalPort)")
                    PortReadyMs = [math]::Round($watch.Elapsed.TotalMilliseconds, 1)
                    Listener = $listener
                }
            }
        } catch {
            $lastError = $_.Exception.Message
        } finally {
            if ($null -ne $client) { $client.Dispose() }
        }
        Start-Sleep -Milliseconds 100
    }
    if ([string]::IsNullOrWhiteSpace($lastError)) {
        throw "DeckPipe owned loopback listener did not become ready within $TimeoutSeconds seconds."
    }
    throw $lastError
}

function Wait-DeckPipeWindowHandle {
    param(
        [Parameter(Mandatory)][Diagnostics.Process]$Process,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )

    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        try {
            $Process.Refresh()
            if ($Process.HasExited) {
                throw 'DeckPipe shell exited before creating its main window.'
            }
            if ($Process.MainWindowHandle -ne [IntPtr]::Zero) {
                return [pscustomobject]@{
                    Handle = $Process.MainWindowHandle
                    ElapsedMs = [math]::Round($watch.Elapsed.TotalMilliseconds, 1)
                }
            }
        } catch [InvalidOperationException] {
            throw 'DeckPipe shell exited before creating its main window.'
        }
        Start-Sleep -Milliseconds 100
    }
    throw "DeckPipe window did not become ready within $TimeoutSeconds seconds."
}

function Invoke-DeckPipeMeasuredGet {
    param(
        [Parameter(Mandatory)][Net.Http.HttpClient]$Client,
        [Parameter(Mandatory)]$Endpoint,
        [Parameter(Mandatory)][uri]$RootUri,
        [Parameter(Mandatory)][int]$SampleCount
    )

    Assert-DeckPipeReadOnlyRequest -Method GET -Path $Endpoint.Path | Out-Null
    $durations = [Collections.Generic.List[double]]::new()
    $statusCodes = [Collections.Generic.List[int]]::new()
    $lastBytes = [byte[]]::new(0)
    $lastContentType = ''

    for ($index = 0; $index -lt $SampleCount; $index++) {
        $requestUri = [uri]::new($RootUri, $Endpoint.Path)
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $response = $Client.GetAsync($requestUri).GetAwaiter().GetResult()
        try {
            $lastBytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            $watch.Stop()
            $durations.Add([math]::Round($watch.Elapsed.TotalMilliseconds, 3))
            $statusCodes.Add([int]$response.StatusCode)
            if ($null -ne $response.Content.Headers.ContentType) {
                $lastContentType = $response.Content.Headers.ContentType.ToString()
            }
        } finally {
            $response.Dispose()
        }
    }

    $body = [Text.Encoding]::UTF8.GetString($lastBytes)
    $payload = $null
    $parseOk = $Endpoint.Path -in @('/', '/__deckpipe_qa_missing__')
    if ($Endpoint.Path -ne '/') {
        try {
            $payload = $body | ConvertFrom-Json -Depth 50
            $parseOk = $true
        } catch {
            $parseOk = $false
        }
    }
    $summary = if ($Endpoint.Path -eq '/') {
        $flags = Get-DeckPipeStringFlags -Value $body
        [pscustomobject][ordered]@{
            kind = 'html'
            utf8_replacement_detected = [bool]($body.Contains([string][char]0xFFFD))
            bad_glyph_detected = $flags.HasBadGlyph
        }
    } else {
        Get-DeckPipePayloadSummary -Endpoint $Endpoint.Path -Payload $payload
    }

    [pscustomobject]@{
        Path = $Endpoint.Path
        ExpectedStatus = [int]$Endpoint.ExpectedStatus
        StatusCodes = $statusCodes.ToArray()
        ResponseBytes = $lastBytes.Length
        ContentType = $lastContentType
        ParseOk = $parseOk
        Metrics = Get-DeckPipeMetricSummary -Values $durations.ToArray()
        Summary = $summary
        Payload = $payload
    }
}

function Get-DeckPipeSensitiveFieldCount {
    param([AllowNull()]$InputObject)

    $state = [pscustomobject]@{ Count = 0 }
    function Visit-DeckPipeObject {
        param([AllowNull()]$Value)
        if ($null -eq $Value -or $Value -is [string]) { return }
        if ($Value -is [Collections.IDictionary]) {
            foreach ($entry in $Value.GetEnumerator()) {
                $name = [string]$entry.Key
                if ($name -ne 'arl_set' -and $name -match '(?i)(arl|oauth|token|password|cookie|secret|authorization)') {
                    $state.Count++
                }
                Visit-DeckPipeObject -Value $entry.Value
            }
            return
        }
        if ($Value -is [Collections.IEnumerable]) {
            foreach ($item in $Value) { Visit-DeckPipeObject -Value $item }
            return
        }
        foreach ($property in $Value.PSObject.Properties) {
            if ($property.MemberType -notin @('NoteProperty', 'Property', 'AliasProperty', 'ScriptProperty')) { continue }
            if ($property.Name -ne 'arl_set' -and $property.Name -match '(?i)(arl|oauth|token|password|cookie|secret|authorization)') {
                $state.Count++
            }
            Visit-DeckPipeObject -Value $property.Value
        }
    }
    Visit-DeckPipeObject -Value $InputObject
    return $state.Count
}

function Initialize-DeckPipeNativeWindowApi {
    if ($null -ne ('DeckPipeQaNativeWindow' -as [type])) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class DeckPipeQaNativeWindow {
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetWindowRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetClientRect(IntPtr hWnd, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int x, int y, int width, int height, uint flags);
}
'@
}

function Set-DeckPipeClientSize {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][int]$Width,
        [Parameter(Mandatory)][int]$Height
    )

    Initialize-DeckPipeNativeWindowApi
    $outer = [DeckPipeQaNativeWindow+RECT]::new()
    $client = [DeckPipeQaNativeWindow+RECT]::new()
    if (-not [DeckPipeQaNativeWindow]::GetWindowRect($Handle, [ref]$outer)) {
        throw 'Unable to read DeckPipe window bounds.'
    }
    if (-not [DeckPipeQaNativeWindow]::GetClientRect($Handle, [ref]$client)) {
        throw 'Unable to read DeckPipe client bounds.'
    }
    $frameWidth = ($outer.Right - $outer.Left) - ($client.Right - $client.Left)
    $frameHeight = ($outer.Bottom - $outer.Top) - ($client.Bottom - $client.Top)
    $flags = 0x0002 -bor 0x0004 -bor 0x0010
    if (-not [DeckPipeQaNativeWindow]::SetWindowPos(
        $Handle, [IntPtr]::Zero, 0, 0, $Width + $frameWidth, $Height + $frameHeight, $flags)) {
        throw 'Unable to resize DeckPipe window.'
    }
    Start-Sleep -Milliseconds 900
    $actual = [DeckPipeQaNativeWindow+RECT]::new()
    [DeckPipeQaNativeWindow]::GetClientRect($Handle, [ref]$actual) | Out-Null
    [pscustomobject]@{
        width = $actual.Right - $actual.Left
        height = $actual.Bottom - $actual.Top
    }
}

function Test-DeckPipeElementFocusable {
    param([Parameter(Mandatory)][Windows.Automation.AutomationElement]$Element)

    $walker = [Windows.Automation.TreeWalker]::ControlViewWalker
    $current = $Element
    $states = [Collections.Generic.List[object]]::new()
    for ($depth = 0; $depth -lt 6 -and $null -ne $current; $depth++) {
        try {
            $controlType = [string]$current.Current.ControlType.ProgrammaticName
            $controlType = $controlType.Replace('ControlType.', '')
            $states.Add([pscustomobject]@{
                control_type = $controlType
                focusable = [bool]$current.Current.IsKeyboardFocusable
            })
            if ($controlType -in @('Document', 'Window')) { break }
            $current = $walker.GetParent($current)
        } catch {
            return $false
        }
    }
    return Test-DeckPipeKeyboardReachable -AncestorStates $states.ToArray()
}

function Get-DeckPipeUiSnapshot {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][object[]]$RequiredControls,
        [string[]]$PlaylistTitles = @()
    )

    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $window = [Windows.Automation.AutomationElement]::FromHandle($Handle)
    $all = $window.FindAll(
        [Windows.Automation.TreeScope]::Descendants,
        [Windows.Automation.Condition]::TrueCondition)

    $elements = [Collections.Generic.List[object]]::new()
    $focusableCount = 0
    $emptyFocusableCount = 0
    $cyrillicNameCount = 0
    $badGlyphNameCount = 0
    foreach ($element in $all) {
        try {
            $name = [string]$element.Current.Name
            $focusable = [bool]$element.Current.IsKeyboardFocusable
            $offscreen = [bool]$element.Current.IsOffscreen
            if ($focusable) {
                $focusableCount++
                if ([string]::IsNullOrWhiteSpace($name)) { $emptyFocusableCount++ }
            }
            if (-not [string]::IsNullOrWhiteSpace($name)) {
                $flags = Get-DeckPipeStringFlags -Value $name
                if ($flags.HasCyrillic) { $cyrillicNameCount++ }
                if ($flags.HasBadGlyph) { $badGlyphNameCount++ }
            }
            $elements.Add([pscustomobject]@{
                Element = $element
                Name = $name
                Focusable = $focusable
                Offscreen = $offscreen
            })
        } catch {
            # Ignore transient WebView accessibility nodes.
        }
    }

    $missing = [Collections.Generic.List[string]]::new()
    $requiredVisible = 0
    $requiredFocusable = 0
    foreach ($required in $RequiredControls) {
        $matches = @($elements | Where-Object { $_.Name -ceq $required.Name })
        if ($matches.Count -eq 0) {
            $missing.Add([string]$required.Id)
            continue
        }
        if (@($matches | Where-Object { -not $_.Offscreen }).Count -gt 0) { $requiredVisible++ }
        if (@($matches | Where-Object { $_.Focusable }).Count -gt 0) { $requiredFocusable++ }
    }

    $playlistFound = 0
    $playlistKeyboardAccessible = 0
    foreach ($title in @($PlaylistTitles | Select-Object -Unique)) {
        if ([string]::IsNullOrWhiteSpace($title)) { continue }
        $match = $elements | Where-Object { $_.Name -ceq $title } | Select-Object -First 1
        if ($null -eq $match) { continue }
        $playlistFound++
        if (Test-DeckPipeElementFocusable -Element $match.Element) {
            $playlistKeyboardAccessible++
        }
    }

    [pscustomobject][ordered]@{
        descendant_count = $elements.Count
        focusable_count = $focusableCount
        empty_focusable_name_count = $emptyFocusableCount
        cyrillic_name_count = $cyrillicNameCount
        bad_glyph_name_count = $badGlyphNameCount
        required_control_count = $RequiredControls.Count
        required_visible_count = $requiredVisible
        required_focusable_count = $requiredFocusable
        missing_control_ids = $missing.ToArray()
        playlist_title_count = @($PlaylistTitles | Select-Object -Unique).Count
        playlist_title_found_count = $playlistFound
        playlist_keyboard_accessible_count = $playlistKeyboardAccessible
    }
}

function Wait-DeckPipeUiSnapshot {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][object[]]$RequiredControls,
        [string[]]$PlaylistTitles = @(),
        [ValidateRange(1, 15)][int]$TimeoutSeconds = 8
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    $lastSnapshot = $null
    while ([DateTime]::UtcNow -lt $deadline) {
        $lastSnapshot = Get-DeckPipeUiSnapshot -Handle $Handle -RequiredControls $RequiredControls -PlaylistTitles $PlaylistTitles
        if (Test-DeckPipeUiSnapshotReady -Snapshot $lastSnapshot -MinimumDescendants 50) {
            return $lastSnapshot
        }
        Start-Sleep -Milliseconds 250
    }
    return $lastSnapshot
}

function Invoke-DeckPipeUiTab {
    param(
        [Parameter(Mandatory)][IntPtr]$Handle,
        [Parameter(Mandatory)][string]$TabId,
        [Parameter(Mandatory)][string]$ButtonName,
        [Parameter(Mandatory)][string]$ExpectedStatePattern,
        [ValidateRange(1, 10)][int]$TimeoutSeconds = 5
    )

    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $window = [Windows.Automation.AutomationElement]::FromHandle($Handle)
    $nameCondition = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::NameProperty, $ButtonName)
    $typeCondition = [Windows.Automation.PropertyCondition]::new(
        [Windows.Automation.AutomationElement]::ControlTypeProperty,
        [Windows.Automation.ControlType]::Button)
    $condition = [Windows.Automation.AndCondition]::new($nameCondition, $typeCondition)
    $button = $window.FindFirst([Windows.Automation.TreeScope]::Descendants, $condition)
    if ($null -eq $button) {
        return [pscustomobject]@{ tab_id = $TabId; invoked = $false; state_visible = $false }
    }

    try {
        $invoke = [Windows.Automation.InvokePattern]$button.GetCurrentPattern(
            [Windows.Automation.InvokePattern]::Pattern)
        $invoke.Invoke()
    } catch {
        return [pscustomobject]@{ tab_id = $TabId; invoked = $false; state_visible = $false }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 200
        $all = $window.FindAll(
            [Windows.Automation.TreeScope]::Descendants,
            [Windows.Automation.Condition]::TrueCondition)
        foreach ($element in $all) {
            try {
                if ([string]$element.Current.Name -match $ExpectedStatePattern) {
                    return [pscustomobject]@{ tab_id = $TabId; invoked = $true; state_visible = $true }
                }
            } catch {
                # Ignore transient WebView accessibility nodes.
            }
        }
    }
    return [pscustomobject]@{ tab_id = $TabId; invoked = $true; state_visible = $false }
}

function Get-DeckPipeProcessMetrics {
    param([Parameter(Mandatory)][int]$RootProcessId)

    function Get-DeckPipeProcessCpuSeconds {
        param([AllowNull()]$Process)
        if ($null -eq $Process -or $null -eq $Process.CPU) { return 0.0 }
        return [double]$Process.CPU
    }

    $inventory = Get-DeckPipeProcessInventory
    $ids = @(Get-DeckPipeDescendantProcessIds -RootProcessId $RootProcessId -Processes $inventory)
    $beforeCpu = @{}
    foreach ($id in $ids) {
        $process = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($null -ne $process) { $beforeCpu[$id] = Get-DeckPipeProcessCpuSeconds -Process $process }
    }
    Start-Sleep -Seconds 2

    $workingSet = 0L
    $privateMemory = 0L
    $cpuDeltaSeconds = 0.0
    $alive = 0
    foreach ($id in $ids) {
        $process = Get-Process -Id $id -ErrorAction SilentlyContinue
        if ($null -eq $process) { continue }
        $alive++
        $workingSet += [long]$process.WorkingSet64
        $privateMemory += [long]$process.PrivateMemorySize64
        $currentCpu = Get-DeckPipeProcessCpuSeconds -Process $process
        $prior = if ($beforeCpu.ContainsKey($id)) { [double]$beforeCpu[$id] } else { $currentCpu }
        $cpuDeltaSeconds += [math]::Max(0, $currentCpu - $prior)
    }

    [pscustomobject][ordered]@{
        process_count = $alive
        working_set_mb = [math]::Round($workingSet / 1MB, 1)
        private_memory_mb = [math]::Round($privateMemory / 1MB, 1)
        idle_cpu_ms_over_2s = [math]::Round($cpuDeltaSeconds * 1000.0, 1)
        captured_process_ids = $ids
    }
}

function Get-DeckPipeBackupCount {
    param([Parameter(Mandatory)][string]$DatabasePath)
    $directory = Split-Path -Parent $DatabasePath
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) { return 0 }
    return @(Get-ChildItem -LiteralPath $directory -Filter 'master.db.deckpipe-backup-*' -File -ErrorAction SilentlyContinue).Count
}

function New-DeckPipeIsolatedProfile {
    $root = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-isolated-qa-' + [guid]::NewGuid().ToString('N'))
    $appData = Join-Path $root 'AppData\Roaming'
    $localAppData = Join-Path $root 'AppData\Local'
    $deckPipeData = Join-Path $appData 'DeckPipe'
    $rekordboxData = Join-Path $appData 'Pioneer\rekordbox'
    $music = Join-Path $root 'Music'
    foreach ($directory in @($deckPipeData, $rekordboxData, $localAppData, $music)) {
        [IO.Directory]::CreateDirectory($directory) | Out-Null
    }
    $configPath = Join-Path $deckPipeData 'config.local.json'
    $fixtureConfig = [ordered]@{
        music_root = $music
        wav_mode = 'source'
        numbering = $true
        sc_sources = @()
        local_sources = @()
    }
    [IO.File]::WriteAllText(
        $configPath,
        (($fixtureConfig | ConvertTo-Json -Depth 5) + "`n"),
        [Text.UTF8Encoding]::new($false))

    [pscustomobject]@{
        Root = $root
        AppData = $appData
        LocalAppData = $localAppData
        ConfigPath = $configPath
        DatabasePath = Join-Path $rekordboxData 'master.db'
    }
}

function New-DeckPipeLiveProfilePaths {
    if ([string]::IsNullOrWhiteSpace($env:APPDATA)) {
        throw 'APPDATA is not available for installed QA.'
    }
    [pscustomobject]@{
        ConfigPath = Join-Path $env:APPDATA 'DeckPipe\config.local.json'
        DatabasePath = Join-Path $env:APPDATA 'Pioneer\rekordbox\master.db'
    }
}

function Assert-DeckPipeIsolationRoot {
    param([Parameter(Mandatory)][string]$Path)

    $resolvedIsolation = [IO.Path]::GetFullPath($Path)
    $resolvedTempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
        [IO.Path]::DirectorySeparatorChar,
        [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $isExpectedIsolationPath = $resolvedIsolation.StartsWith(
        $resolvedTempBase, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolvedIsolation).StartsWith(
            'deckpipe-isolated-qa-', [StringComparison]::OrdinalIgnoreCase)
    if (-not $isExpectedIsolationPath) {
        throw 'Refusing to remove an unexpected isolation directory.'
    }
    return $resolvedIsolation
}

function Stop-DeckPipeOwnedProcesses {
    param(
        [Parameter(Mandatory)][int[]]$CandidateProcessIds,
        [Parameter(Mandatory)][string]$InstallDirectory
    )

    $stopped = [Collections.Generic.List[int]]::new()
    $inventory = Get-DeckPipeProcessInventory
    foreach ($candidate in @($inventory | Where-Object { [int]$_.ProcessId -in $CandidateProcessIds })) {
        if (-not (Test-DeckPipeProcessPathOwned -ProcessPath $candidate.ExecutablePath -InstallDirectory $InstallDirectory)) {
            continue
        }
        Stop-Process -Id ([int]$candidate.ProcessId) -Force -ErrorAction SilentlyContinue
        $stopped.Add([int]$candidate.ProcessId)
    }
    return $stopped.ToArray()
}

if (-not (Test-Path -LiteralPath $budgetPath -PathType Leaf)) {
    throw "Performance budget was not found: $budgetPath"
}
$budget = Get-Content -LiteralPath $budgetPath -Raw | ConvertFrom-Json
$endpoints = @(Get-DeckPipeReadOnlyEndpoints)

function Get-DeckPipeRunSummary {
    param(
        [Parameter(Mandatory)][object[]]$Checks,
        [switch]$ForceBlocked
    )

    $mandatoryIds = @('A3', 'A4', 'A5', 'B1', 'B2', 'B4', 'B5', 'D2')
    $passed = @($Checks | Where-Object { $_.status -eq 'pass' }).Count
    $failed = @($Checks | Where-Object { $_.status -eq 'fail' }).Count
    $warnings = @($Checks | Where-Object { $_.status -eq 'warn' }).Count
    $skipped = @($Checks | Where-Object { $_.status -eq 'skipped' }).Count
    $blockers = @($Checks | Where-Object { $_.status -eq 'skipped' -and $_.id -in $mandatoryIds } | ForEach-Object { [string]$_.id })
    if ($ForceBlocked -and @($blockers | Where-Object { $_ -eq 'RELEASE' }).Count -eq 0) {
        $blockers += 'RELEASE'
    }
    $overallStatus = if ($failed -gt 0) { 'fail' } elseif ($blockers.Count -gt 0) { 'blocked' } elseif ($warnings -gt 0) { 'warn' } else { 'pass' }
    [ordered]@{
        status = $overallStatus
        passed = $passed
        failed = $failed
        warnings = $warnings
        skipped = $skipped
        blocked = $blockers.Count
        blockers = $blockers
    }
}

function Invoke-DeckPipeVerifierProcessContractSelfTest {
    $root = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-verifier-process-' + [guid]::NewGuid().ToString('N'))
    $stage = Join-Path $root 'stage'
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    $utf8 = [Text.UTF8Encoding]::new($false)
    $scripts = [ordered]@{
        pass = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"PASS","message":"ok"}'
exit 0
"@
        fail = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"FAIL","message":"bad"}'
exit 1
"@
        blocked = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"BLOCKED","message":"blocked"}'
exit 2
"@
        empty = @"
param([string]`$StagingDirectory)
exit 0
"@
        multiple = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"PASS","message":"one"}'
Write-Output '{"schema_version":1,"status":"PASS","message":"two"}'
exit 0
"@
        noise = @"
param([string]`$StagingDirectory)
Write-Output 'noise'
Write-Output '{"schema_version":1,"status":"PASS","message":"ok"}'
exit 0
"@
        pass_stderr = @"
param([string]`$StagingDirectory)
[Console]::Error.WriteLine('unexpected stderr')
Write-Output '{"schema_version":1,"status":"PASS","message":"ok"}'
exit 0
"@
        fail_stderr = @"
param([string]`$StagingDirectory)
[Console]::Error.WriteLine('unexpected stderr')
Write-Output '{"schema_version":1,"status":"FAIL","message":"bad"}'
exit 1
"@
        blocked_stderr = @"
param([string]`$StagingDirectory)
[Console]::Error.WriteLine('unexpected stderr')
Write-Output '{"schema_version":1,"status":"BLOCKED","message":"blocked"}'
exit 2
"@
        pass_exit_1 = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"PASS","message":"ok"}'
exit 1
"@
        fail_exit_0 = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"FAIL","message":"bad"}'
exit 0
"@
        blocked_exit_0 = @"
param([string]`$StagingDirectory)
Write-Output '{"schema_version":1,"status":"BLOCKED","message":"blocked"}'
exit 0
"@
    }
    $results = [ordered]@{}
    try {
        foreach ($name in $scripts.Keys) {
            $scriptPath = Join-Path $root ($name + '.ps1')
            [IO.File]::WriteAllText($scriptPath, [string]$scripts[$name], $utf8)
            try {
                $result = Invoke-DeckPipeReleaseVerifierProcess -StagePath $stage -VerifierPath $scriptPath -TimeoutSeconds 5
                $results[$name] = "$($result.status):$($result.exit_code)"
            } catch {
                $results[$name] = 'REJECTED'
            }
        }
        Write-Host "SELFTEST_JSON $(ConvertTo-Json ([ordered]@{ cases = $results }) -Depth 8 -Compress)"
    } finally {
        if (Test-Path -LiteralPath $root -PathType Container) {
            [IO.Directory]::Delete($root, $true)
        }
    }
}

function Invoke-DeckPipeReparseAttributeGuardSelfTest {
    $cases = [ordered]@{
        candidate_evidence_directory = [IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint
        evidence_top_level_entry = [IO.FileAttributes]::Archive -bor [IO.FileAttributes]::ReparsePoint
        staged_artifact = [IO.FileAttributes]::Archive -bor [IO.FileAttributes]::ReparsePoint
        installed_exe = [IO.FileAttributes]::Archive -bor [IO.FileAttributes]::ReparsePoint
        install_directory = [IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint
        ordinary_file = [IO.FileAttributes]::Archive
    }
    $results = [ordered]@{}
    foreach ($name in $cases.Keys) {
        try {
            Assert-DeckPipeNoReparseAttributes -Attributes $cases[$name] -Context $name
            $results[$name] = 'ACCEPTED'
        } catch {
            $results[$name] = 'REJECTED'
        }
    }
    Write-Host "SELFTEST_JSON $(ConvertTo-Json ([ordered]@{ cases = $results }) -Depth 8 -Compress)"
}

if (-not [string]::IsNullOrWhiteSpace($SelfTestContract)) {
    $selfTestInstall = Join-Path ([IO.Path]::GetTempPath()) 'deckpipe-listener-selftest'
    $selfTestRootExe = Join-Path $selfTestInstall 'DeckPipe.exe'
    $selfTestChildExe = Join-Path $selfTestInstall 'deckpipe-backend.exe'
    $selfTestGrandchildExe = Join-Path $selfTestInstall 'nested\deckpipe-worker.exe'
    $selfTestOutsideExe = Join-Path ([IO.Path]::GetTempPath()) 'outside-helper.exe'
    switch ($SelfTestContract) {
        'listener-owned' {
            $listener = Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId 100 `
                -Processes @(
                    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe },
                    [pscustomobject]@{ ProcessId = 101; ParentProcessId = 100; ExecutablePath = $selfTestChildExe },
                    [pscustomobject]@{ ProcessId = 102; ParentProcessId = 101; ExecutablePath = $selfTestGrandchildExe }
                ) `
                -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; LocalPort = 53123; OwningProcess = 102 }) `
                -InstallDirectory $selfTestInstall
            Write-Host "SELFTEST_JSON $(ConvertTo-Json ([ordered]@{ port = [int]$listener.LocalPort; owner = [int]$listener.OwningProcess }) -Compress)"
            return
        }
        'listener-none' {
            Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId 100 `
                -Processes @([pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe }) `
                -Listeners @() `
                -InstallDirectory $selfTestInstall | Out-Null
            return
        }
        'listener-multiple' {
            Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId 100 `
                -Processes @(
                    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe },
                    [pscustomobject]@{ ProcessId = 101; ParentProcessId = 100; ExecutablePath = $selfTestChildExe },
                    [pscustomobject]@{ ProcessId = 102; ParentProcessId = 101; ExecutablePath = $selfTestGrandchildExe }
                ) `
                -Listeners @(
                    [pscustomobject]@{ LocalAddress = '127.0.0.1'; LocalPort = 53123; OwningProcess = 101 },
                    [pscustomobject]@{ LocalAddress = '::1'; LocalPort = 53124; OwningProcess = 102 }
                ) `
                -InstallDirectory $selfTestInstall | Out-Null
            return
        }
        'listener-unowned' {
            Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId 100 `
                -Processes @([pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe }) `
                -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; LocalPort = 53123; OwningProcess = 999 }) `
                -InstallDirectory $selfTestInstall | Out-Null
            return
        }
        'listener-wrong-path' {
            Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId 100 `
                -Processes @(
                    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe },
                    [pscustomobject]@{ ProcessId = 101; ParentProcessId = 100; ExecutablePath = $selfTestOutsideExe }
                ) `
                -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; LocalPort = 53123; OwningProcess = 101 }) `
                -InstallDirectory $selfTestInstall | Out-Null
            return
        }
        'listener-pid-reuse' {
            $initialListener = Select-DeckPipeOwnedLoopbackListener `
                -RootProcessId 100 `
                -Processes @(
                    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe },
                    [pscustomobject]@{ ProcessId = 101; ParentProcessId = 100; ExecutablePath = $selfTestChildExe }
                ) `
                -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; LocalPort = 53123; OwningProcess = 101 }) `
                -InstallDirectory $selfTestInstall
            Assert-DeckPipeLoopbackListenerStable `
                -RootProcessId 100 `
                -InitialListener $initialListener `
                -InstallDirectory $selfTestInstall `
                -Processes @(
                    [pscustomobject]@{ ProcessId = 100; ParentProcessId = 0; ExecutablePath = $selfTestRootExe },
                    [pscustomobject]@{ ProcessId = 101; ParentProcessId = 999; ExecutablePath = $selfTestOutsideExe }
                ) `
                -Listeners @([pscustomobject]@{ LocalAddress = '127.0.0.1'; LocalPort = 53123; OwningProcess = 101 }) | Out-Null
            return
        }
        'mandatory-skips' {
            $selfChecks = [Collections.Generic.List[object]]::new()
            $selfChecks.Add((New-DeckPipeCheck -Id 'A1' -Status 'pass' -Message 'startup ok' -Data $null))
            $selfChecks.Add((New-DeckPipeCheck -Id 'A3' -Status 'skipped' -Message 'token unavailable' -Data ([ordered]@{ blocker_id = 'bearer-token-memory-only' })))
            $selfChecks.Add((New-DeckPipeCheck -Id 'D2' -Status 'skipped' -Message 'token unavailable' -Data ([ordered]@{ blocker_id = 'bearer-token-memory-only' })))
            Write-Host "SELFTEST_JSON $(ConvertTo-Json ([ordered]@{ summary = (Get-DeckPipeRunSummary -Checks $selfChecks.ToArray()) }) -Depth 8 -Compress)"
            return
        }
        'verifier-process-contract' {
            Invoke-DeckPipeVerifierProcessContractSelfTest
            return
        }
        'reparse-attribute-guard' {
            Invoke-DeckPipeReparseAttributeGuardSelfTest
            return
        }
        'identity' {
            $identity = Assert-DeckPipeCandidateIdentity -Path $ExePath
            Write-Host "SELFTEST_JSON $(ConvertTo-Json ([ordered]@{ identity = $identity; launch_allowed = [bool]$identity.launch_allowed }) -Depth 8 -Compress)"
            return
        }
        'isolated-preflight' {
            $identity = Assert-DeckPipeCandidateIdentity -Path $ExePath
            if (-not $IsolatedUi) {
                throw 'isolated-preflight self-test requires IsolatedUi.'
            }
            $profile = New-DeckPipeIsolatedProfile
            try {
                $configBeforeSelfTest = Get-DeckPipeFileSnapshot -Path $profile.ConfigPath
                $databaseBeforeSelfTest = Get-DeckPipeFileSnapshot -Path $profile.DatabasePath
                $backupCountBeforeSelfTest = Get-DeckPipeBackupCount -DatabasePath $profile.DatabasePath
                Write-Host "SELFTEST_JSON $(ConvertTo-Json ([ordered]@{
                    launch_allowed = [bool]$identity.launch_allowed
                    isolated = $true
                    isolated_root = [string]$profile.Root
                    config_path = [string]$profile.ConfigPath
                    database_path = [string]$profile.DatabasePath
                    backup_count = [int]$backupCountBeforeSelfTest
                    config_exists = [bool]$configBeforeSelfTest.Exists
                    database_exists = [bool]$databaseBeforeSelfTest.Exists
                    identity = $identity
                }) -Depth 8 -Compress)"
            } finally {
                if (Test-Path -LiteralPath $profile.Root -PathType Container) {
                    [IO.Directory]::Delete((Assert-DeckPipeIsolationRoot -Path $profile.Root), $true)
                }
            }
            return
        }
    }
}

if ($ValidateOnly) {
    foreach ($endpoint in $endpoints) {
        Assert-DeckPipeReadOnlyRequest -Method $endpoint.Method -Path $endpoint.Path | Out-Null
    }
    $validationMode = if ($IsolatedUi) { 'isolated-ui' } else { 'installed-read-only' }
    Write-Host "VALID installed-runner mode=$validationMode endpoints=$($endpoints.Count) samples=$Samples"
    return
}

$candidateIdentity = Assert-DeckPipeCandidateIdentity -Path $ExePath
if (-not [bool]$candidateIdentity.launch_allowed) {
    Write-Host "QA_RESULT status=blocked passed=0 failed=0 warnings=0 blocked=1 blockers=RELEASE"
    exit 1
}
$resolvedExe = [string]$candidateIdentity.resolved_exe
$installDirectory = [string]$candidateIdentity.install_directory

$checks = [Collections.Generic.List[object]]::new()
$requestLog = [Collections.Generic.List[object]]::new()
$endpointMetrics = [ordered]@{}
$performance = [ordered]@{}
$runStarted = [DateTimeOffset]::UtcNow
$runId = $(if ($IsolatedUi) { 'isolated-ui-' } else { 'installed-' }) + $runStarted.ToString('yyyyMMdd-HHmmss')
$shellProcess = $null
$windowHandle = [IntPtr]::Zero
$capturedProcessIds = @()
$orphanDetected = $false
$cleanupPortClosed = $false
$unsafeRequestCount = 0
$fatalErrorType = $null
$reportedVersion = $null
$playlistTitles = @()
$uiNormal = $null
$uiMinimum = $null
$isolatedRoot = $null
$isolatedAppData = $null
$isolatedLocalAppData = $null
$isolatedConfigPath = $null
$isolatedConfigBefore = $null
$isolatedConfigUnchanged = $null
$isolationDirectoryRemoved = -not $IsolatedUi
$isolatedTabInteractions = 0
$profilePaths = $null
if ($IsolatedUi) {
    $profilePaths = New-DeckPipeIsolatedProfile
    $isolatedRoot = [string]$profilePaths.Root
    $isolatedAppData = [string]$profilePaths.AppData
    $isolatedLocalAppData = [string]$profilePaths.LocalAppData
    $isolatedConfigPath = [string]$profilePaths.ConfigPath
} else {
    $profilePaths = New-DeckPipeLiveProfilePaths
}
$configPath = [string]$profilePaths.ConfigPath
$databasePath = [string]$profilePaths.DatabasePath
$configBefore = Get-DeckPipeFileSnapshot -Path $configPath
$databaseBefore = Get-DeckPipeFileSnapshot -Path $databasePath
$backupCountBefore = Get-DeckPipeBackupCount -DatabasePath $databasePath
$isolatedConfigBefore = if ($IsolatedUi) { $configBefore } else { $null }

function Add-RunCheck {
    param([string]$Id, [string]$Status, [string]$Message, [AllowNull()]$Data = $null)
    $checks.Add((New-DeckPipeCheck -Id $Id -Status $Status -Message $Message -Data $Data))
}

try {
    $inventory = Get-DeckPipeProcessInventory
    $alreadyRunning = @($inventory | Where-Object {
        Test-DeckPipeProcessPathOwned -ProcessPath $_.ExecutablePath -InstallDirectory $installDirectory
    })
    if ($alreadyRunning.Count -gt 0) {
        throw 'Installed QA preflight found an active DeckPipe process from the candidate directory.'
    }

    $launchWatch = [Diagnostics.Stopwatch]::StartNew()
    if ($IsolatedUi) {
        $startInfo = [Diagnostics.ProcessStartInfo]::new()
        $startInfo.FileName = $resolvedExe
        $startInfo.WorkingDirectory = $installDirectory
        $startInfo.UseShellExecute = $false
        $startInfo.Environment['APPDATA'] = $isolatedAppData
        $startInfo.Environment['LOCALAPPDATA'] = $isolatedLocalAppData
        $shellProcess = [Diagnostics.Process]::Start($startInfo)
    } else {
        $shellProcess = Start-Process -FilePath $resolvedExe -PassThru -WindowStyle Normal
    }
    $listenerReady = Wait-DeckPipeOwnedLoopbackListener -RootProcessId $shellProcess.Id -TimeoutSeconds $StartupTimeoutSeconds -InstallDirectory $installDirectory
    $BaseUri = $listenerReady.BaseUri
    $portReadyMs = [double]$listenerReady.PortReadyMs
    $window = Wait-DeckPipeWindowHandle -Process $shellProcess -TimeoutSeconds $StartupTimeoutSeconds
    $windowHandle = $window.Handle
    $windowReadyMs = [math]::Round($launchWatch.Elapsed.TotalMilliseconds, 1)
    $performance.port_ready_ms = $portReadyMs
    $performance.window_ready_ms = $windowReadyMs
    $startupWithinBudget = $portReadyMs -le [double]$budget.port_ready_ms -and
        $windowReadyMs -le [double]$budget.window_ready_ms
    Add-RunCheck -Id 'A1' -Status $(if ($startupWithinBudget) { 'pass' } else { 'fail' }) `
        -Message $(if ($startupWithinBudget) { 'installed shell and sidecar became ready' } else { 'installed startup exceeded its budget' }) `
        -Data ([ordered]@{ port_ready_ms = $portReadyMs; window_ready_ms = $windowReadyMs })

    $reportedVersion = [string]$candidateIdentity.actual_version
    $versionPass = $reportedVersion -eq [string]$budget.target_version -and
        -not [string]::IsNullOrWhiteSpace([string]$candidateIdentity.actual_build_id)
    Add-RunCheck -Id 'A2' -Status $(if ($versionPass) { 'pass' } else { 'fail' }) `
        -Message $(if ($versionPass) { 'candidate identity matches the QA target before launch' } else { 'candidate identity differs from the QA target' }) `
        -Data ([ordered]@{
            expected_version = [string]$budget.target_version
            actual_version = [string]$candidateIdentity.actual_version
            actual_build_id = [string]$candidateIdentity.actual_build_id
            actual_sha256 = [string]$candidateIdentity.actual_sha256
            source_revision = [string]$candidateIdentity.source_revision
            evidence_directory = [string]$candidateIdentity.evidence_directory
            staged_artifact = [string]$candidateIdentity.staged_artifact_path
            release_verifier_status = [string]$candidateIdentity.release_verifier_status
            engineering_mode = [bool]$candidateIdentity.engineering_mode
        })

    foreach ($flow in @(
        [pscustomobject]@{ Id = 'A3'; Message = 'root document direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'A4'; Message = 'configuration direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'A5'; Message = 'unknown-route direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'B1'; Message = 'Deezer playlist direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'B2'; Message = 'SoundCloud source direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'B4'; Message = 'Rekordbox status direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'B5'; Message = 'error listing direct HTTP sampling requires the in-memory bearer token and is not run by installed QA' },
        [pscustomobject]@{ Id = 'D2'; Message = 'API median performance checks are skipped because installed QA has no token bypass' }
    )) {
        Add-RunCheck -Id $flow.Id -Status 'skipped' -Message $flow.Message `
            -Data ([ordered]@{ reason = 'bearer-token-memory-only'; base_uri = [string]$BaseUri })
    }

    $uiNameSearch = New-DeckPipeUnicodeString -CodePoints @(0x041F, 0x043E, 0x0438, 0x0441, 0x043A)
    $uiNameErrors = New-DeckPipeUnicodeString -CodePoints @(0x041E, 0x0448, 0x0438, 0x0431, 0x043A, 0x0438)
    $uiNameSave = New-DeckPipeUnicodeString -CodePoints @(0x0421, 0x043E, 0x0445, 0x0440, 0x0430, 0x043D, 0x0438, 0x0442, 0x044C)
    $uiNameBugReport = New-DeckPipeUnicodeString -CodePoints @(0x1F41E, 0x20, 0x0411, 0x0430, 0x0433, 0x0440, 0x0435, 0x043F, 0x043E, 0x0440, 0x0442)
    $uiWordChoose = New-DeckPipeUnicodeString -CodePoints @(0x0412, 0x044B, 0x0431, 0x0435, 0x0440, 0x0438, 0x0442, 0x0435)
    $uiWordPlaylist = New-DeckPipeUnicodeString -CodePoints @(0x043F, 0x043B, 0x0435, 0x0439, 0x043B, 0x0438, 0x0441, 0x0442)
    $uiWordSource = New-DeckPipeUnicodeString -CodePoints @(0x0438, 0x0441, 0x0442, 0x043E, 0x0447, 0x043D, 0x0438, 0x043A)
    $uiWordLeft = New-DeckPipeUnicodeString -CodePoints @(0x0441, 0x043B, 0x0435, 0x0432, 0x0430)
    $uiWordLeftTitle = New-DeckPipeUnicodeString -CodePoints @(0x0421, 0x043B, 0x0435, 0x0432, 0x0430)
    $uiWordTarget = New-DeckPipeUnicodeString -CodePoints @(0x0446, 0x0435, 0x043B, 0x044C)
    $uiWordTracks = New-DeckPipeUnicodeString -CodePoints @(0x0422, 0x0440, 0x0435, 0x043A, 0x0438)
    $uiWordWith = New-DeckPipeUnicodeString -CodePoints @(0x0441)
    $uiWordErrorsPlural = New-DeckPipeUnicodeString -CodePoints @(0x043E, 0x0448, 0x0438, 0x0431, 0x043A, 0x0430, 0x043C, 0x0438)
    $uiWordNoErrors = New-DeckPipeUnicodeString -CodePoints @(0x041E, 0x0448, 0x0438, 0x0431, 0x043E, 0x043A)
    $uiWordNone = New-DeckPipeUnicodeString -CodePoints @(0x043D, 0x0435, 0x0442)
    $requiredControls = @(
        [pscustomobject]@{ Id = 'deezer_tab'; Name = 'Deezer' },
        [pscustomobject]@{ Id = 'soundcloud_tab'; Name = 'SoundCloud' },
        [pscustomobject]@{ Id = 'search_tab'; Name = $uiNameSearch },
        [pscustomobject]@{ Id = 'errors_tab'; Name = $uiNameErrors },
        [pscustomobject]@{ Id = 'save'; Name = $uiNameSave },
        [pscustomobject]@{ Id = 'bug_report'; Name = $uiNameBugReport }
    )
    $normalSize = Set-DeckPipeClientSize -Handle $windowHandle -Width 1320 -Height 840
    $uiNormal = Wait-DeckPipeUiSnapshot -Handle $windowHandle -RequiredControls $requiredControls -PlaylistTitles $playlistTitles
    $normalPass = $normalSize.width -ge 1310 -and $normalSize.height -ge 830 -and
        $uiNormal.required_visible_count -eq $uiNormal.required_control_count
    Add-RunCheck -Id 'C1' -Status $(if ($normalPass) { 'pass' } else { 'fail' }) `
        -Message $(if ($normalPass) { 'main controls are visible at normal size' } else { 'normal-size window clips or misses required controls' }) `
        -Data ([ordered]@{ client_size = $normalSize; ui = $uiNormal })

    $minimumSize = Set-DeckPipeClientSize -Handle $windowHandle -Width 1000 -Height 640
    $uiMinimum = Wait-DeckPipeUiSnapshot -Handle $windowHandle -RequiredControls $requiredControls -PlaylistTitles $playlistTitles
    $minimumPass = $minimumSize.width -ge 990 -and $minimumSize.height -ge 630 -and
        $uiMinimum.required_visible_count -eq $uiMinimum.required_control_count
    Add-RunCheck -Id 'C2' -Status $(if ($minimumPass) { 'pass' } else { 'fail' }) `
        -Message $(if ($minimumPass) { 'main controls remain visible at minimum size' } else { 'minimum-size window clips or hides required controls' }) `
        -Data ([ordered]@{ client_size = $minimumSize; ui = $uiMinimum })

    $minimumA11yPass = $minimumSize.width -ge 990 -and $minimumSize.height -ge 630 -and
        $uiMinimum.required_focusable_count -eq $uiMinimum.required_control_count -and
        @($uiMinimum.missing_control_ids).Count -eq 0
    $minimumA11yStatus = if (-not $IsolatedUi) { 'warn' } elseif ($minimumA11yPass) { 'pass' } else { 'fail' }
    Add-RunCheck -Id 'C2A' -Status $minimumA11yStatus `
        -Message $(if (-not $IsolatedUi) { 'minimum-size accessibility check requires the synthetic isolated UI fixture' } elseif ($minimumA11yPass) { 'minimum-size synthetic required controls remain visible and keyboard-focusable' } else { 'minimum-size synthetic UI is missing required focusable controls' }) `
        -Data ([ordered]@{ synthetic_isolated_ui = [bool]$IsolatedUi; client_size = $minimumSize; required_focusable_count = $uiMinimum.required_focusable_count; required_control_count = $uiMinimum.required_control_count; empty_focusable_name_count = $uiMinimum.empty_focusable_name_count; missing_control_ids = $uiMinimum.missing_control_ids })

    Set-DeckPipeClientSize -Handle $windowHandle -Width 1320 -Height 840 | Out-Null
    $tabsReady = $uiNormal.required_visible_count -ge 4 -and $uiNormal.required_focusable_count -ge 4
    if ($IsolatedUi -and $tabsReady) {
        $tabResults = @(
            Invoke-DeckPipeUiTab -Handle $windowHandle -TabId 'deezer' -ButtonName 'Deezer' -ExpectedStatePattern ($uiWordChoose + '\s+' + $uiWordPlaylist + '\s+' + $uiWordLeft)
            Invoke-DeckPipeUiTab -Handle $windowHandle -TabId 'soundcloud' -ButtonName 'SoundCloud' -ExpectedStatePattern ($uiWordChoose + '\s+' + $uiWordSource + '\s+' + $uiWordLeft)
            Invoke-DeckPipeUiTab -Handle $windowHandle -TabId 'search' -ButtonName $uiNameSearch -ExpectedStatePattern ($uiWordLeftTitle + '\s+' + (New-DeckPipeUnicodeString -CodePoints @(0x2014)) + '\s+' + $uiWordTarget)
            Invoke-DeckPipeUiTab -Handle $windowHandle -TabId 'errors' -ButtonName $uiNameErrors -ExpectedStatePattern ($uiWordTracks + '\s+' + $uiWordWith + '\s+' + $uiWordErrorsPlural + '|' + $uiWordNoErrors + '\s+' + $uiWordNone)
        )
        $isolatedTabInteractions = @($tabResults | Where-Object invoked).Count
        $allInvoked = @($tabResults | Where-Object { -not $_.invoked }).Count -eq 0
        $allStatesVisible = @($tabResults | Where-Object { -not $_.state_visible }).Count -eq 0
        Add-RunCheck -Id 'C3' -Status $(if ($allInvoked) { 'pass' } else { 'fail' }) `
            -Message $(if ($allInvoked) { 'all tabs activate in the isolated profile' } else { 'one or more tabs could not be activated in the isolated profile' }) `
            -Data ([ordered]@{ tabs = $tabResults })
        Add-RunCheck -Id 'C4' -Status $(if ($allStatesVisible) { 'pass' } else { 'fail' }) `
            -Message $(if ($allStatesVisible) { 'each isolated tab exposes an explanatory state' } else { 'one or more isolated tabs lack the expected explanatory state' }) `
            -Data ([ordered]@{ tabs = $tabResults })
    } else {
        Add-RunCheck -Id 'C3' -Status $(if ($tabsReady) { 'warn' } else { 'fail' }) `
            -Message $(if ($tabsReady) { 'tab controls are present; activation is reserved for isolated QA' } else { 'one or more tab controls are unavailable' }) `
            -Data ([ordered]@{ isolated_activation_required = $true })
        Add-RunCheck -Id 'C4' -Status 'warn' -Message 'state transitions are reserved for isolated QA'
    }

    $playlistCount = $uiNormal.playlist_title_count
    $keyboardCount = $uiNormal.playlist_keyboard_accessible_count
    $accessibilityStatus = if ($playlistCount -eq 0) { 'warn' } elseif ($keyboardCount -eq $playlistCount) { 'pass' } else { 'fail' }
    Add-RunCheck -Id 'C5' -Status $accessibilityStatus `
        -Message $(if ($playlistCount -eq 0) { 'no playlist cards were available for keyboard checks' } elseif ($keyboardCount -eq $playlistCount) { 'all located playlist cards are keyboard-accessible' } else { 'one or more playlist cards are not keyboard-accessible' }) `
        -Data ([ordered]@{ playlist_count = $playlistCount; located_count = $uiNormal.playlist_title_found_count; keyboard_accessible_count = $keyboardCount; empty_focusable_name_count = $uiNormal.empty_focusable_name_count })

    $payloadBadGlyphs = 0
    $payloadCyrillic = 0
    foreach ($metric in $endpointMetrics.Values) {
        $bad = Get-PropertyValue -InputObject $metric.payload -Name 'bad_glyph_string_count' -Default 0
        $cyr = Get-PropertyValue -InputObject $metric.payload -Name 'cyrillic_string_count' -Default 0
        $payloadBadGlyphs += [int]$bad
        $payloadCyrillic += [int]$cyr
    }
    $totalBadGlyphs = $payloadBadGlyphs + [int]$uiNormal.bad_glyph_name_count
    $totalCyrillic = $payloadCyrillic + [int]$uiNormal.cyrillic_name_count
    $unicodeStatus = if ($totalBadGlyphs -gt 0) { 'fail' } elseif ($totalCyrillic -gt 0) { 'pass' } else { 'warn' }
    Add-RunCheck -Id 'C6' -Status $unicodeStatus `
        -Message $(if ($totalBadGlyphs -gt 0) { 'replacement or square glyphs were detected' } elseif ($totalCyrillic -gt 0) { 'Cyrillic is present without detected replacement glyphs' } else { 'no Cyrillic sample was available in the live data' }) `
        -Data ([ordered]@{ cyrillic_string_count = $totalCyrillic; bad_glyph_string_count = $totalBadGlyphs })

    $processMetrics = Get-DeckPipeProcessMetrics -RootProcessId $shellProcess.Id
    $capturedProcessIds = @($processMetrics.captured_process_ids)
    $performance.process_count = $processMetrics.process_count
    $performance.working_set_mb = $processMetrics.working_set_mb
    $performance.private_memory_mb = $processMetrics.private_memory_mb
    $performance.idle_cpu_ms_over_2s = $processMetrics.idle_cpu_ms_over_2s
    $footprintPass = $processMetrics.working_set_mb -le [double]$budget.working_set_mb -and
        $processMetrics.private_memory_mb -le [double]$budget.private_memory_mb -and
        $processMetrics.idle_cpu_ms_over_2s -le [double]$budget.idle_cpu_ms_over_2s
    Add-RunCheck -Id 'D1' -Status $(if ($performance.window_ready_ms -le [double]$budget.window_ready_ms) { 'pass' } else { 'fail' }) `
        -Message $(if ($performance.window_ready_ms -le [double]$budget.window_ready_ms) { 'window-ready time is within budget' } else { 'window-ready time exceeds budget' }) `
        -Data ([ordered]@{ window_ready_ms = $performance.window_ready_ms; budget_ms = [double]$budget.window_ready_ms })
    Add-RunCheck -Id 'D3' -Status $(if ($footprintPass) { 'pass' } else { 'fail' }) `
        -Message $(if ($footprintPass) { 'idle process footprint is within budget' } else { 'idle process footprint exceeds budget' }) `
        -Data ([ordered]@{ process_count = $processMetrics.process_count; working_set_mb = $processMetrics.working_set_mb; private_memory_mb = $processMetrics.private_memory_mb; idle_cpu_ms_over_2s = $processMetrics.idle_cpu_ms_over_2s })
} catch {
    $fatalErrorType = $_.Exception.GetType().Name
    Add-RunCheck -Id 'RUNNER' -Status 'fail' -Message "installed QA stopped after a $fatalErrorType"
} finally {
    if ($null -ne $shellProcess) {
        try {
            $inventoryBeforeClose = Get-DeckPipeProcessInventory
            $capturedProcessIds = @(Get-DeckPipeDescendantProcessIds -RootProcessId $shellProcess.Id -Processes $inventoryBeforeClose)
            $shellProcess.Refresh()
            if (-not $shellProcess.HasExited) {
                $shellProcess.CloseMainWindow() | Out-Null
            }
            $graceDeadline = [DateTime]::UtcNow.AddSeconds(8)
            while ([DateTime]::UtcNow -lt $graceDeadline) {
                $alive = @(Get-Process -Id $capturedProcessIds -ErrorAction SilentlyContinue)
                $portClosedDuringGrace = $true
                if ($null -ne $BaseUri) {
                    $portClosedDuringGrace = $null -eq (Get-DeckPipePortOwner -Port $BaseUri.Port)
                }
                if ($alive.Count -eq 0 -and $portClosedDuringGrace) { break }
                Start-Sleep -Milliseconds 250
            }
            $ownedAlive = @((Get-DeckPipeProcessInventory) | Where-Object {
                [int]$_.ProcessId -in $capturedProcessIds -and
                (Test-DeckPipeProcessPathOwned -ProcessPath $_.ExecutablePath -InstallDirectory $installDirectory)
            })
            if ($ownedAlive.Count -gt 0) {
                $orphanDetected = $true
                Stop-DeckPipeOwnedProcesses -CandidateProcessIds @($ownedAlive.ProcessId) -InstallDirectory $installDirectory | Out-Null
                Start-Sleep -Milliseconds 700
            }
            if ($null -ne $BaseUri) {
                $portOwnerAfterStop = Get-DeckPipePortOwner -Port $BaseUri.Port
                if ($null -ne $portOwnerAfterStop) {
                    $portProcess = (Get-DeckPipeProcessInventory | Where-Object { [int]$_.ProcessId -eq $portOwnerAfterStop } | Select-Object -First 1)
                    if ($null -ne $portProcess -and
                        (Test-DeckPipeProcessPathOwned -ProcessPath $portProcess.ExecutablePath -InstallDirectory $installDirectory)) {
                        Stop-DeckPipeOwnedProcesses -CandidateProcessIds @($portOwnerAfterStop) -InstallDirectory $installDirectory | Out-Null
                        $orphanDetected = $true
                        Start-Sleep -Milliseconds 700
                    }
                }
            }
        } catch {
            if ($null -eq $fatalErrorType) { $fatalErrorType = $_.Exception.GetType().Name }
        }
    }

    $cleanupPortClosed = $true
    if ($null -ne $BaseUri) {
        $cleanupPortClosed = $null -eq (Get-DeckPipePortOwner -Port $BaseUri.Port)
    }
    $lingeringOwned = @((Get-DeckPipeProcessInventory) | Where-Object {
        [int]$_.ProcessId -in @($capturedProcessIds) -and
        (Test-DeckPipeProcessPathOwned -ProcessPath $_.ExecutablePath -InstallDirectory $installDirectory)
    })
    $configAfter = Get-DeckPipeFileSnapshot -Path $configPath
    $databaseAfter = Get-DeckPipeFileSnapshot -Path $databasePath
    $backupCountAfter = Get-DeckPipeBackupCount -DatabasePath $databasePath
    $configUnchanged = Test-DeckPipeFileSnapshotEqual -Before $configBefore -After $configAfter
    $databaseUnchanged = Test-DeckPipeFileSnapshotEqual -Before $databaseBefore -After $databaseAfter
    $backupCountUnchanged = $backupCountBefore -eq $backupCountAfter

    if ($IsolatedUi -and $null -ne $isolatedConfigBefore -and $null -ne $isolatedConfigPath) {
        $isolatedConfigAfter = Get-DeckPipeFileSnapshot -Path $isolatedConfigPath
        $isolatedConfigUnchanged = Test-DeckPipeFileSnapshotEqual -Before $isolatedConfigBefore -After $isolatedConfigAfter
    }
    if ($IsolatedUi -and $null -ne $isolatedRoot -and (Test-Path -LiteralPath $isolatedRoot -PathType Container)) {
        try {
            $resolvedIsolation = [IO.Path]::GetFullPath($isolatedRoot)
            $resolvedTempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
                [IO.Path]::DirectorySeparatorChar,
                [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
            $isExpectedIsolationPath = $resolvedIsolation.StartsWith(
                $resolvedTempBase, [StringComparison]::OrdinalIgnoreCase) -and
                [IO.Path]::GetFileName($resolvedIsolation).StartsWith(
                    'deckpipe-isolated-qa-', [StringComparison]::OrdinalIgnoreCase)
            if (-not $isExpectedIsolationPath) {
                throw 'Refusing to remove an unexpected isolation directory.'
            }
            [IO.Directory]::Delete($resolvedIsolation, $true)
            $isolationDirectoryRemoved = -not [IO.Directory]::Exists($resolvedIsolation)
        } catch {
            $isolationDirectoryRemoved = $false
            if ($null -eq $fatalErrorType) { $fatalErrorType = $_.Exception.GetType().Name }
        }
    }

    $protectedFilesPass = $configUnchanged -and $databaseUnchanged -and
        (-not $IsolatedUi -or $isolatedConfigUnchanged)
    Add-RunCheck -Id 'E1' -Status $(if ($protectedFilesPass) { 'pass' } else { 'fail' }) `
        -Message $(if ($protectedFilesPass) { 'protected user files are unchanged' } else { 'a protected file changed during QA' }) `
        -Data ([ordered]@{ config_unchanged = $configUnchanged; rekordbox_database_unchanged = $databaseUnchanged; isolated_fixture_config_unchanged = $isolatedConfigUnchanged })
    Add-RunCheck -Id 'E2' -Status $(if ($unsafeRequestCount -eq 0 -and $backupCountUnchanged) { 'pass' } else { 'fail' }) `
        -Message $(if ($unsafeRequestCount -eq 0 -and $backupCountUnchanged) { 'only allowlisted GET requests ran and no Rekordbox backup was created' } else { 'the external-write safety boundary was violated' }) `
        -Data ([ordered]@{ allowlisted_get_count = $requestLog.Count; unsafe_request_count = $unsafeRequestCount; isolated_ui_interaction_count = $isolatedTabInteractions; rekordbox_backup_count_unchanged = $backupCountUnchanged })

    $cleanupPass = $cleanupPortClosed -and $lingeringOwned.Count -eq 0 -and -not $orphanDetected -and
        $isolationDirectoryRemoved
    Add-RunCheck -Id 'E3' -Status $(if ($cleanupPass) { 'pass' } else { 'fail' }) `
        -Message $(if ($cleanupPass) { 'DeckPipe exited cleanly and released its port' } elseif ($cleanupPortClosed -and $lingeringOwned.Count -eq 0) { 'DeckPipe left an owned process after graceful close; the runner removed it' } else { 'DeckPipe cleanup left an owned process or listening port' }) `
        -Data ([ordered]@{ orphan_detected = $orphanDetected; lingering_owned_process_count = $lingeringOwned.Count; port_closed = $cleanupPortClosed; isolation_directory_removed = $isolationDirectoryRemoved })

    $summary = Get-DeckPipeRunSummary -Checks $checks.ToArray() -ForceBlocked:([bool]$candidateIdentity.engineering_mode)
    $overallStatus = [string]$summary.status
    $run = [ordered]@{
        schema_version = 1
        run_id = $runId
        generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        target = [ordered]@{
            product = 'DeckPipe'
            version = $reportedVersion
            build_id = [string]$candidateIdentity.actual_build_id
            executable = [IO.Path]::GetFileName($resolvedExe)
            executable_sha256 = [string]$candidateIdentity.actual_sha256
            staged_executable_sha256 = [string]$candidateIdentity.staged_sha256
            identity_match_mode = [string]$candidateIdentity.identity_match_mode
            accepted_bundle_type = [string]$candidateIdentity.accepted_bundle_type
            source_revision = [string]$candidateIdentity.source_revision
            evidence_directory = [string]$candidateIdentity.evidence_directory
            staged_artifact = [string]$candidateIdentity.staged_artifact_path
            release_verifier_status = [string]$candidateIdentity.release_verifier_status
            engineering_mode = [bool]$candidateIdentity.engineering_mode
            mode = $(if ($IsolatedUi) { 'isolated-ui' } else { 'installed-read-only' })
        }
        safety = [ordered]@{
            live_mutation_endpoints_called = 0
            protected_config_unchanged = $configUnchanged
            rekordbox_database_unchanged = $databaseUnchanged
            rekordbox_backup_count_unchanged = $backupCountUnchanged
            isolated_fixture_config_unchanged = $isolatedConfigUnchanged
            isolation_directory_removed = $isolationDirectoryRemoved
        }
        summary = $summary
        checks = $checks.ToArray()
        performance = $performance
        endpoint_metrics = $endpointMetrics
        fatal_error_type = $fatalErrorType
    }
    $artifacts = Write-DeckPipeRunArtifacts -Run $run -OutputDirectory $OutputDirectory
    Write-Host "QA_RESULT status=$overallStatus passed=$($summary.passed) failed=$($summary.failed) warnings=$($summary.warnings) blocked=$($summary.blocked)"
    Write-Host "QA_JSON $($artifacts.Json)"
    Write-Host "QA_MARKDOWN $($artifacts.Markdown)"
}

if ((@($checks | Where-Object { $_.status -eq 'fail' }).Count -gt 0 -or $overallStatus -eq 'blocked') -and -not $NoFailOnFindings) {
    exit 1
}
