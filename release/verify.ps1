param(
    [string]$StagingDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$script:MetadataFiles = @('release-evidence.json', 'sbom.spdx.json', 'SHA256SUMS.txt')

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

function Read-CanonicalVersion {
    $versionPath = Join-Path $PSScriptRoot 'version.json'
    if (-not (Test-Path -LiteralPath $versionPath -PathType Leaf)) { throw 'canonical release version is missing' }
    $version = Get-Content -LiteralPath $versionPath -Raw | ConvertFrom-Json
    foreach ($required in @('product', 'version', 'build_id', 'artifact_name_prefix')) {
        if (-not ($version.PSObject.Properties.Name -contains $required)) { throw "canonical release version missing $required" }
    }
    if ($version.artifact_name_prefix -ne "DeckPipe-$($version.build_id)") { throw 'canonical release artifact prefix mismatch' }
    return $version
}

function Assert-GitSourceRevision {
    param([string]$Revision, [string]$Context)
    $value = if ($null -eq $Revision) { '' } else { [string]$Revision }
    $value = $value.Trim()
    if ($value -cnotmatch '^[0-9a-f]{40}$') {
        throw "source provenance BLOCKED: $Context did not contain a full lowercase revision"
    }
    return $value
}

function Resolve-GitMetadataPath {
    param([string]$BasePath, [string]$GitPath)
    if ([string]::IsNullOrWhiteSpace($GitPath)) { throw 'source provenance BLOCKED: git metadata path is empty' }
    $candidate = [string]$GitPath
    if (-not [IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $BasePath $candidate
    }
    return [IO.Path]::GetFullPath($candidate)
}

function Read-GitMetadataFirstLine {
    param([string]$Path, [string]$Context)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "source provenance BLOCKED: $Context is missing"
    }
    $lines = @(Get-Content -LiteralPath $Path -TotalCount 1)
    if ($lines.Count -eq 0 -or [string]::IsNullOrWhiteSpace([string]$lines[0])) {
        throw "source provenance BLOCKED: $Context is empty"
    }
    return ([string]$lines[0]).Trim()
}

function Test-SafeGitRefName {
    param([string]$RefName)
    if ([string]::IsNullOrWhiteSpace($RefName)) { return $false }
    $name = [string]$RefName
    if ($name -cne $name.Trim()) { return $false }
    if ($name -eq '@') { return $false }
    if ($name.IndexOf('\') -ge 0) { return $false }
    if ($name -match '[\x00-\x20\x7f~^:?*\[]') { return $false }
    if ([IO.Path]::IsPathRooted($name)) { return $false }
    if ($name -match '^[A-Za-z]:') { return $false }
    if ($name -match '\.\.|@\{|//') { return $false }
    if ($name.StartsWith('/')) { return $false }
    if ($name.EndsWith('/')) { return $false }
    if ($name.EndsWith('.', [StringComparison]::Ordinal)) { return $false }
    if ($name.EndsWith('.lock', [StringComparison]::OrdinalIgnoreCase)) { return $false }
    $parts = @($name -split '/')
    if ($parts.Count -lt 2) { return $false }
    foreach ($part in $parts) {
        if ([string]::IsNullOrEmpty($part)) { return $false }
        if ($part -eq '.' -or $part -eq '..') { return $false }
        if ($part.StartsWith('.', [StringComparison]::Ordinal)) { return $false }
        if ($part.EndsWith('.', [StringComparison]::Ordinal)) { return $false }
        if ($part.EndsWith('.lock', [StringComparison]::OrdinalIgnoreCase)) { return $false }
    }
    return $true
}

function Get-CaseSensitiveChildPath {
    param(
        [string]$Directory,
        [string]$Name,
        [switch]$File,
        [switch]$DirectoryOnly
    )
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return '' }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Directory -Force)) {
        if ($entry.Name -cne $Name) { continue }
        if ($File -and $entry.PSIsContainer) { continue }
        if ($DirectoryOnly -and -not $entry.PSIsContainer) { continue }
        return $entry.FullName
    }
    return ''
}

function Get-GitLooseRefRevision {
    param([string]$GitDirectory, [string]$RefName)
    $current = $GitDirectory
    $parts = @($RefName -split '/')
    for ($index = 0; $index -lt ($parts.Count - 1); $index++) {
        $current = Get-CaseSensitiveChildPath -Directory $current -Name $parts[$index] -DirectoryOnly
        if (-not $current) { return '' }
    }
    $refPath = Get-CaseSensitiveChildPath -Directory $current -Name $parts[$parts.Count - 1] -File
    if (-not $refPath) { return '' }
    $revision = Read-GitMetadataFirstLine -Path $refPath -Context "git ref $RefName"
    return (Assert-GitSourceRevision -Revision $revision -Context "git ref $RefName")
}

function Get-GitPackedRefRevisions {
    param([string]$GitDirectory, [string]$RefName)
    $packedRefsPath = Join-Path $GitDirectory 'packed-refs'
    if (-not (Test-Path -LiteralPath $packedRefsPath -PathType Leaf)) { return @() }
    $revisions = @()
    $lineNumber = 0
    foreach ($line in @(Get-Content -LiteralPath $packedRefsPath)) {
        $lineNumber++
        $trimmed = ([string]$line).Trim()
        if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
        if ($trimmed.StartsWith('^')) {
            if ($trimmed -cnotmatch '^\^[0-9a-f]{40}$') {
                throw "source provenance BLOCKED: malformed packed ref line $lineNumber"
            }
            continue
        }
        if ($trimmed -cnotmatch '^([0-9a-f]{40}) ([^\s]+)$') {
            throw "source provenance BLOCKED: malformed packed ref line $lineNumber"
        }
        $packedRevision = $Matches[1]
        $packedRefName = $Matches[2]
        if (-not (Test-SafeGitRefName $packedRefName)) {
            throw "source provenance BLOCKED: unsafe packed ref name $packedRefName"
        }
        if ([string]::Equals($packedRefName, $RefName, [StringComparison]::OrdinalIgnoreCase) -and
            -not [string]::Equals($packedRefName, $RefName, [StringComparison]::Ordinal)) {
            throw "source provenance BLOCKED: case-confusable packed ref $packedRefName"
        }
        if ([string]::Equals($packedRefName, $RefName, [StringComparison]::Ordinal)) {
            $revisions += (Assert-GitSourceRevision -Revision $packedRevision -Context "packed ref $RefName")
        }
    }
    if ($revisions.Count -gt 1) {
        throw "source provenance BLOCKED: duplicate packed ref $RefName"
    }
    return $revisions
}

function Get-GitRefRevision {
    param([string[]]$GitDirectories, [string]$RefName)
    if (-not (Test-SafeGitRefName $RefName)) {
        throw "source provenance BLOCKED: unsafe git ref name $RefName"
    }
    $uniqueGitDirectories = @()
    $seenGitDirectories = @{}
    foreach ($gitDirectory in @($GitDirectories)) {
        if ([string]::IsNullOrWhiteSpace($gitDirectory)) { continue }
        $fullGitDirectory = [IO.Path]::GetFullPath([string]$gitDirectory).TrimEnd('\', '/')
        $key = $fullGitDirectory.ToUpperInvariant()
        if (-not $seenGitDirectories.ContainsKey($key)) {
            $seenGitDirectories[$key] = $true
            $uniqueGitDirectories += $fullGitDirectory
        }
    }
    foreach ($gitDirectory in @($uniqueGitDirectories)) {
        $revision = Get-GitLooseRefRevision -GitDirectory $gitDirectory -RefName $RefName
        if ($revision) { return $revision }
    }
    $packedRevisions = @()
    foreach ($gitDirectory in @($uniqueGitDirectories)) {
        $packedRevisions += @(Get-GitPackedRefRevisions -GitDirectory $gitDirectory -RefName $RefName)
    }
    if ($packedRevisions.Count -gt 1) {
        throw "source provenance BLOCKED: ambiguous packed ref $RefName"
    }
    if ($packedRevisions.Count -eq 1) {
        return $packedRevisions[0]
    }
    throw "source provenance BLOCKED: git ref $RefName is unavailable"
}

function Get-CurrentSourceRevision {
    $repoRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($script:RepoRoot)
    $dotGit = Join-Path $repoRoot '.git'
    if (Test-Path -LiteralPath $dotGit -PathType Container) {
        $gitDir = [IO.Path]::GetFullPath($dotGit)
    } elseif (Test-Path -LiteralPath $dotGit -PathType Leaf) {
        $gitFile = Read-GitMetadataFirstLine -Path $dotGit -Context '.git file'
        if ($gitFile -cnotmatch '^gitdir:\s*(.+)$') {
            throw 'source provenance BLOCKED: .git file does not point to gitdir'
        }
        $gitDir = Resolve-GitMetadataPath -BasePath $repoRoot -GitPath $Matches[1]
    } else {
        throw 'source provenance BLOCKED: .git metadata is missing'
    }
    if (-not (Test-Path -LiteralPath $gitDir -PathType Container)) {
        throw 'source provenance BLOCKED: gitdir is unavailable'
    }

    $commonDir = $gitDir
    $commonDirFile = Join-Path $gitDir 'commondir'
    if (Test-Path -LiteralPath $commonDirFile -PathType Leaf) {
        $commonDir = Resolve-GitMetadataPath -BasePath $gitDir -GitPath (Read-GitMetadataFirstLine -Path $commonDirFile -Context 'commondir')
        if (-not (Test-Path -LiteralPath $commonDir -PathType Container)) {
            throw 'source provenance BLOCKED: commondir is unavailable'
        }
    }

    $head = Read-GitMetadataFirstLine -Path (Join-Path $gitDir 'HEAD') -Context 'HEAD'
    if ($head -cmatch '^[0-9a-f]{40}$') {
        return (Assert-GitSourceRevision -Revision $head -Context 'HEAD')
    }
    if ($head -cnotmatch '^ref:\s*(.+)$') {
        throw 'source provenance BLOCKED: HEAD is malformed'
    }
    $refName = [string]$Matches[1]
    return (Get-GitRefRevision -GitDirectories @($gitDir, $commonDir) -RefName $refName)
}

function Get-ReleaseRelativePath {
    param([string]$RelativePath)
    if (-not $RelativePath) { throw 'empty release path' }
    $path = ($RelativePath -replace '\\', '/')
    if ($path -match '^[A-Za-z]:|^/|//|:') { throw "path traversal, ADS, or escape in release path: $RelativePath" }
    $parts = @($path -split '/' | Where-Object { $_ -ne '' })
    if ($parts.Count -eq 0) { throw 'empty release path' }
    foreach ($part in $parts) {
        if ($part -eq '.' -or $part -eq '..') { throw "path traversal or escape in release path: $RelativePath" }
    }
    if ($parts.Count -ne 1) { throw "nested release paths are not allowed: $RelativePath" }
    return $parts[0]
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

function Get-ArtifactType {
    param([string]$RelativePath)
    $extension = [IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
    if ($extension -eq '.exe') { return 'exe' }
    if ($extension -eq '.msi') { return 'msi' }
    throw "unexpected artifact extension: $RelativePath"
}

function Test-IsArtifactPath {
    param([string]$RelativePath)
    $extension = [IO.Path]::GetExtension($RelativePath).ToLowerInvariant()
    return ($extension -eq '.exe' -or $extension -eq '.msi')
}

function Assert-CanonicalArtifactName {
    param(
        [string]$RelativePath,
        $CanonicalVersion,
        [string]$ExpectedSourceRevision
    )
    $shortRevision = $ExpectedSourceRevision.Substring(0, 7)
    $prefix = [regex]::Escape([string]$CanonicalVersion.artifact_name_prefix) + '-' + [regex]::Escape($shortRevision)
    $pattern = '^' + $prefix + '-x64(\.exe|-setup\.exe|\.msi)$'
    if ($RelativePath -notmatch $pattern) {
        throw "ambiguous executable artifact or non-canonical artifact name for canonical build/current HEAD: $RelativePath"
    }
}

function Assert-ClosedTopLevelStaging {
    param([string]$StagePath)
    $stageItem = Get-Item -LiteralPath $StagePath -Force
    if (($stageItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'staging directory reparse point is not allowed' }
    if (-not $stageItem.PSIsContainer) { throw 'staging path must be a directory' }

    $seen = @{}
    $files = @()
    foreach ($entry in @(Get-ChildItem -LiteralPath $StagePath -Force | Sort-Object Name)) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "staging reparse point is not allowed: $($entry.Name)" }
        if ($entry.PSIsContainer) { throw "staging subdirectories are not allowed: $($entry.Name)" }
        $relative = Assert-NoForbiddenPath $entry.Name
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "duplicate or case-confusable staged path: $relative" }
        $seen[$key] = $true

        $metadataMatch = @($script:MetadataFiles | Where-Object { $_ -ieq $relative })
        if ($metadataMatch.Count -gt 0 -and $metadataMatch[0] -cne $relative) {
            throw "case-confusable release metadata path: $relative"
        }
        if ($metadataMatch.Count -eq 0 -and -not (Test-IsArtifactPath $relative)) {
            throw "extra staged file outside release allowlist: $relative"
        }
        $files += [pscustomobject]@{ RelativePath = $relative; FullName = $entry.FullName; Sha256 = Get-Sha256 $entry.FullName }
    }
    foreach ($required in $script:MetadataFiles) {
        if (-not $seen.ContainsKey($required.ToLowerInvariant())) { throw "required release file is missing: $required" }
    }
    return $files
}

function Get-TopLevelStageFileRecords {
    param([string]$StagePath)
    $files = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $StagePath -Force -File | Sort-Object Name)) {
        $relative = Assert-NoForbiddenPath $file.Name
        $files += [pscustomobject]@{ RelativePath = $relative; FullName = $file.FullName; Sha256 = Get-Sha256 $file.FullName }
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
        if ($relative -eq 'SHA256SUMS.txt') { throw 'SHA-256 manifest must not contain itself' }
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "duplicate or case-confusable manifest path: $relative" }
        $seen[$key] = $true
        $entries += [pscustomobject]@{ RelativePath = $relative; Sha256 = $Matches[1] }
    }
    return $entries
}

function Assert-KeySetsEqual {
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

function ConvertTo-PathSet {
    param($Items)
    $set = @{}
    foreach ($item in @($Items)) {
        $relative = Assert-NoForbiddenPath $item.RelativePath
        $set[$relative.ToLowerInvariant()] = $relative
    }
    return $set
}

function Assert-ManifestExact {
    param(
        [string]$StagePath,
        $ActualFiles = $null
    )
    $entries = Read-ManifestEntries $StagePath
    $actualList = @(Get-TopLevelStageFileRecords $StagePath)
    $actualByPath = @{}
    foreach ($file in @($actualList | Where-Object { $_.RelativePath -ne 'SHA256SUMS.txt' })) {
        $actualByPath[$file.RelativePath.ToLowerInvariant()] = $file.RelativePath
    }
    $manifestByPath = @{}
    foreach ($entry in $entries) { $manifestByPath[$entry.RelativePath.ToLowerInvariant()] = $entry.RelativePath }
    Assert-KeySetsEqual -Expected $actualByPath -Actual $manifestByPath -Context 'manifest/staging inventory mismatch'

    $actualHashByPath = @{}
    foreach ($file in $actualList) { $actualHashByPath[$file.RelativePath.ToLowerInvariant()] = $file.Sha256 }
    foreach ($entry in $entries) {
        if ($actualHashByPath[$entry.RelativePath.ToLowerInvariant()] -ne $entry.Sha256) {
            throw "manifest hash mismatch: $($entry.RelativePath)"
        }
    }
    return $entries
}

function Read-ReleaseEvidence {
    param(
        $StagePath,
        $ManifestEntries = $null,
        $CanonicalVersion = $null,
        [string]$ExpectedSourceRevision = ''
    )
    if ($null -eq $ManifestEntries) {
        $ManifestEntries = $StagePath
        $stagePathForEvidence = $script:CurrentStagePath
    } else {
        $stagePathForEvidence = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath([string]$StagePath)
    }
    if (-not $stagePathForEvidence) { throw 'release evidence stage path is missing' }
    $evidencePath = Join-Path $stagePathForEvidence 'release-evidence.json'
    if (-not (Test-Path -LiteralPath $evidencePath)) { throw 'release evidence is missing' }
    $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
    foreach ($required in @('schema_version', 'product', 'version', 'build_id', 'source_revision', 'artifacts', 'signing', 'timestamp')) {
        if (-not ($evidence.PSObject.Properties.Name -contains $required)) { throw "malformed release evidence: missing $required" }
    }
    foreach ($required in @('status', 'signed')) {
        if (-not ($evidence.signing.PSObject.Properties.Name -contains $required)) { throw "malformed release evidence: signing missing $required" }
    }
    if (-not ($evidence.timestamp.PSObject.Properties.Name -contains 'status')) { throw 'malformed release evidence: timestamp missing status' }
    if ($null -eq $CanonicalVersion) { $CanonicalVersion = Read-CanonicalVersion }
    if (-not $ExpectedSourceRevision) { $ExpectedSourceRevision = Get-CurrentSourceRevision }
    if ($evidence.product -ne $CanonicalVersion.product) { throw 'malformed release evidence: product mismatch with canonical version' }
    if ($evidence.version -ne $CanonicalVersion.version) { throw 'malformed release evidence: version mismatch with canonical version' }
    if ($evidence.build_id -ne $CanonicalVersion.build_id) { throw 'malformed release evidence: build_id mismatch with canonical version' }
    if ($evidence.source_revision -ne $ExpectedSourceRevision) { throw 'malformed release evidence: source_revision mismatch with current HEAD' }

    $artifacts = @($evidence.artifacts)
    $signed = @($evidence.signing.signed)
    if ($artifacts.Count -eq 0) { throw 'release evidence artifacts must be nonempty' }
    if ($signed.Count -eq 0) { throw 'release evidence signing.signed must be nonempty' }

    $manifestByPath = @{}
    foreach ($entry in $ManifestEntries) { $manifestByPath[$entry.RelativePath.ToLowerInvariant()] = $entry }
    $artifactByPath = @{}
    foreach ($artifact in $artifacts) {
        foreach ($required in @('path', 'sha256', 'type')) {
            if (-not ($artifact.PSObject.Properties.Name -contains $required)) { throw "malformed release evidence: artifact missing $required" }
        }
        $relative = Assert-NoForbiddenPath $artifact.path
        Assert-CanonicalArtifactName -RelativePath $relative -CanonicalVersion $CanonicalVersion -ExpectedSourceRevision $ExpectedSourceRevision
        $expectedType = Get-ArtifactType $relative
        if ([string]$artifact.type -ne $expectedType) { throw "malformed release evidence: artifact type/extension mismatch $relative" }
        $key = $relative.ToLowerInvariant()
        if ($artifactByPath.ContainsKey($key)) { throw "duplicate or case-confusable evidence artifact path: $relative" }
        $artifactByPath[$key] = $relative
        if (-not $manifestByPath.ContainsKey($key)) { throw "malformed release evidence: artifact missing from manifest $relative" }
        if ($manifestByPath[$key].Sha256 -ne $artifact.sha256) { throw "malformed release evidence: artifact hash mismatch $relative" }
    }

    $signedByPath = @{}
    foreach ($signedPath in $signed) {
        $relative = Assert-NoForbiddenPath $signedPath
        $key = $relative.ToLowerInvariant()
        if ($signedByPath.ContainsKey($key)) { throw "duplicate or case-confusable signed artifact path: $relative" }
        $signedByPath[$key] = $relative
    }
    Assert-KeySetsEqual -Expected $artifactByPath -Actual $signedByPath -Context 'signed/artifact evidence mismatch'
    return $evidence
}

function Assert-ArtifactInventoryExact {
    param(
        $ActualFiles,
        $ManifestEntries,
        $Evidence
    )
    $actualArtifacts = @($ActualFiles | Where-Object { Test-IsArtifactPath $_.RelativePath })
    if ($actualArtifacts.Count -eq 0) { throw 'At least one signed executable or installer artifact is required' }
    $actualByPath = ConvertTo-PathSet $actualArtifacts

    $evidenceByPath = @{}
    foreach ($artifact in @($Evidence.artifacts)) {
        $relative = Assert-NoForbiddenPath $artifact.path
        $evidenceByPath[$relative.ToLowerInvariant()] = $relative
    }

    $manifestArtifacts = @($ManifestEntries | Where-Object { Test-IsArtifactPath $_.RelativePath })
    $manifestByPath = ConvertTo-PathSet $manifestArtifacts
    Assert-KeySetsEqual -Expected $actualByPath -Actual $evidenceByPath -Context 'actual/evidence artifact mismatch'
    Assert-KeySetsEqual -Expected $actualByPath -Actual $manifestByPath -Context 'actual/manifest artifact mismatch'
}

function Assert-SbomExact {
    param(
        [string]$StagePath,
        $ManifestEntries,
        $Evidence = $null
    )
    if ($null -eq $Evidence) { $Evidence = Read-ReleaseEvidence -StagePath $StagePath -ManifestEntries $ManifestEntries }
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
    if (-not $expected.ContainsKey('release-evidence.json')) { throw 'SBOM expected set must include release evidence' }

    $sbomByPath = @{}
    $sbomArtifactByPath = @{}
    $fileIds = @{}
    foreach ($file in @($sbom.files)) {
        $relative = Assert-NoForbiddenPath $file.fileName
        $key = $relative.ToLowerInvariant()
        if ($fileIds.ContainsKey($file.SPDXID)) { throw "duplicate SPDXID: $($file.SPDXID)" }
        if ($sbomByPath.ContainsKey($key)) { throw "duplicate or case-confusable SBOM file path: $relative" }
        $fileIds[$file.SPDXID] = $true
        $sbomByPath[$key] = $relative
        if (-not $expected.ContainsKey($key)) { throw "SBOM contains unexpected file: $relative" }
        $shaEntries = @($file.checksums | Where-Object { $_.algorithm -eq 'SHA256' })
        if ($shaEntries.Count -ne 1 -or $shaEntries[0].checksumValue -ne $expected[$key].Sha256) { throw "SBOM checksum mismatch: $relative" }
        $contains = @($sbom.relationships | Where-Object { $_.spdxElementId -eq 'SPDXRef-Package-DeckPipe' -and $_.relationshipType -eq 'CONTAINS' -and $_.relatedSpdxElement -eq $file.SPDXID })
        if ($contains.Count -ne 1) { throw "SBOM missing Package CONTAINS File relationship: $relative" }
        if (Test-IsArtifactPath $relative) { $sbomArtifactByPath[$key] = $relative }
    }

    $allowedRelationships = @{}
    $allowedRelationships['SPDXRef-DOCUMENT|DESCRIBES|SPDXRef-Package-DeckPipe'] = $true
    foreach ($file in @($sbom.files)) {
        $allowedRelationships["SPDXRef-Package-DeckPipe|CONTAINS|$($file.SPDXID)"] = $true
    }
    $seenRelationships = @{}
    foreach ($relationship in @($sbom.relationships)) {
        $key = "$($relationship.spdxElementId)|$($relationship.relationshipType)|$($relationship.relatedSpdxElement)"
        if (-not $allowedRelationships.ContainsKey($key)) { throw "SPDX SBOM contains extra relationship: $key" }
        if ($seenRelationships.ContainsKey($key)) { throw "SPDX SBOM contains duplicate relationship: $key" }
        $seenRelationships[$key] = $true
    }
    Assert-KeySetsEqual -Expected $allowedRelationships -Actual $seenRelationships -Context 'SPDX relationship set mismatch'

    $expectedByPath = @{}
    foreach ($key in @($expected.Keys)) { $expectedByPath[$key] = $expected[$key].RelativePath }
    Assert-KeySetsEqual -Expected $expectedByPath -Actual $sbomByPath -Context 'SBOM/manifest file inventory mismatch'

    $evidenceArtifactByPath = @{}
    foreach ($artifact in @($Evidence.artifacts)) {
        $relative = Assert-NoForbiddenPath $artifact.path
        $evidenceArtifactByPath[$relative.ToLowerInvariant()] = $relative
    }
    Assert-KeySetsEqual -Expected $evidenceArtifactByPath -Actual $sbomArtifactByPath -Context 'SBOM/evidence artifact mismatch'
}

function Get-SignedReleaseArtifacts {
    param($ActualFiles)
    return @($ActualFiles | Where-Object { Test-IsArtifactPath $_.RelativePath } | Sort-Object RelativePath)
}

function Invoke-ReleaseVerification {
    param(
        [string]$StagingDirectory,
        [scriptblock]$SignatureProbe,
        [string]$ExpectedSourceRevision
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
        $canonicalVersion = Read-CanonicalVersion
        if (-not $ExpectedSourceRevision) { $ExpectedSourceRevision = Get-CurrentSourceRevision }
    } catch {
        return New-Result 'BLOCKED' $_.Exception.Message
    }

    try {
        $script:CurrentStagePath = $stagePath
        $actualFiles = Assert-ClosedTopLevelStaging $stagePath
        $manifestEntries = Assert-ManifestExact -StagePath $stagePath -ActualFiles $actualFiles
        $evidence = Read-ReleaseEvidence -StagePath $stagePath -ManifestEntries $manifestEntries -CanonicalVersion $canonicalVersion -ExpectedSourceRevision $ExpectedSourceRevision
        Assert-ArtifactInventoryExact -ActualFiles $actualFiles -ManifestEntries $manifestEntries -Evidence $evidence
        Assert-SbomExact -StagePath $stagePath -ManifestEntries $manifestEntries -Evidence $evidence
        if ($evidence.signing.status -ne 'PASS' -or $evidence.timestamp.status -ne 'PASS') {
            return New-Result 'BLOCKED' 'Release evidence does not contain PASS signing and timestamp status'
        }
        foreach ($artifact in @(Get-SignedReleaseArtifacts $actualFiles)) {
            $signature = & $SignatureProbe $artifact.FullName
            if ($signature.Status -ne 'Valid') { return New-Result 'BLOCKED' "Authenticode signature is not valid for $($artifact.RelativePath)" }
            if ($null -eq $signature.TimeStamperCertificate) { return New-Result 'BLOCKED' "Authenticode timestamp is missing for $($artifact.RelativePath)" }
        }
        return New-Result 'PASS' 'Release evidence, manifest, SBOM, Authenticode signatures, and timestamps are valid'
    } catch {
        return New-Result 'FAIL' $_.Exception.Message
    } finally {
        $script:CurrentStagePath = $null
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    $result = Invoke-ReleaseVerification -StagingDirectory $StagingDirectory
    $result | ConvertTo-Json -Depth 6
    if ($result.status -eq 'FAIL') { exit 1 }
    if ($result.status -eq 'BLOCKED') { exit 2 }
    exit 0
}
