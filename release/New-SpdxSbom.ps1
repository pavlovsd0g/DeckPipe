param(
    [Parameter(Mandatory = $true)]
    [string]$InputDirectory,

    [Parameter(Mandatory = $true)]
    [string]$OutputPath,

    [Parameter(Mandatory = $true)]
    [string]$VersionJsonPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

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

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-SpdxId {
    param([string]$RelativePath, [string]$Checksum)
    $bytes = [Text.Encoding]::UTF8.GetBytes($RelativePath + [char]0 + $Checksum)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = (($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') }) -join '')
        return "SPDXRef-File-$($hash.Substring(0, 32))"
    } finally {
        $sha.Dispose()
    }
}

function Assert-NoForbiddenPath {
    param([string]$RelativePath)
    $normalized = ($RelativePath -replace '\\', '/')
    if ($normalized -match '(^|/)(config\.local\.json|cookies?\.txt|master\.db)$') { throw "forbidden SBOM input: $RelativePath" }
    if ($normalized -match '(?i)(credential|secret|token|cookie|profile|appdata|localappdata|rekordbox|master\.db|\.sqlite|\.db$|\.media$)') {
        throw "forbidden SBOM input: $RelativePath"
    }
    if ($normalized -match '^[A-Za-z]:|^/|(^|/)\.\.(/|$)') { throw "path traversal in SBOM input: $RelativePath" }
    return $normalized
}

$inputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InputDirectory)
$version = Get-Content -LiteralPath $VersionJsonPath -Raw | ConvertFrom-Json
$outputFullPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)

$files = @()
$relationships = @([ordered]@{
    spdxElementId = 'SPDXRef-DOCUMENT'
    relationshipType = 'DESCRIBES'
    relatedSpdxElement = 'SPDXRef-Package-DeckPipe'
})
$seenPaths = @{}
$seenIds = @{}
$allFiles = @(Get-ChildItem -LiteralPath $inputPath -Recurse -Force -File |
    Where-Object { $_.FullName -ne $outputFullPath -and $_.Name -ne 'SHA256SUMS.txt' } |
    Sort-Object FullName)
foreach ($file in $allFiles) {
    $relative = $file.FullName.Substring($inputPath.Length).TrimStart('\') -replace '\\', '/'
    $relative = Assert-NoForbiddenPath $relative
    $pathKey = $relative.ToLowerInvariant()
    if ($seenPaths.ContainsKey($pathKey)) { throw "duplicate or case-confusable SBOM path: $relative" }
    $seenPaths[$pathKey] = $true
    $checksum = Get-Sha256 $file.FullName
    $fileId = Get-SpdxId -RelativePath $relative -Checksum $checksum
    if ($seenIds.ContainsKey($fileId)) { throw "duplicate SPDXID: $fileId" }
    $seenIds[$fileId] = $true
    $files += [ordered]@{
        SPDXID = $fileId
        fileName = $relative
        checksums = @([ordered]@{ algorithm = 'SHA256'; checksumValue = $checksum })
        licenseConcluded = 'NOASSERTION'
        copyrightText = 'NOASSERTION'
    }
    $relationships += [ordered]@{
        spdxElementId = 'SPDXRef-Package-DeckPipe'
        relationshipType = 'CONTAINS'
        relatedSpdxElement = $fileId
    }
}

$sbom = [ordered]@{
    spdxVersion = 'SPDX-2.3'
    dataLicense = 'CC0-1.0'
    SPDXID = 'SPDXRef-DOCUMENT'
    name = "DeckPipe-$($version.build_id)"
    documentNamespace = "https://deckpipe.local/spdx/$($version.build_id)"
    creationInfo = [ordered]@{
        created = $version.build_utc
        creators = @('Tool: DeckPipe release/New-SpdxSbom.ps1')
    }
    packages = @([ordered]@{
        SPDXID = 'SPDXRef-Package-DeckPipe'
        name = 'DeckPipe'
        versionInfo = $version.version
        downloadLocation = 'NOASSERTION'
        filesAnalyzed = $true
        licenseConcluded = 'NOASSERTION'
        licenseDeclared = 'NOASSERTION'
        copyrightText = 'NOASSERTION'
    })
    files = $files
    relationships = $relationships
}

Write-Utf8NoBom $outputFullPath ($sbom | ConvertTo-Json -Depth 10)
