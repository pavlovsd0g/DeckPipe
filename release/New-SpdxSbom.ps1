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

$inputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($InputDirectory)
$version = Get-Content -LiteralPath $VersionJsonPath -Raw | ConvertFrom-Json
$outputFullPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)

$forbiddenPatterns = @('config.local.json', '*.secure.json', '*.sqlite', '*.db', '*.cookie', '*.cookies', '*credential*', '*secret*', '*token*')
foreach ($pattern in $forbiddenPatterns) {
    $hit = Get-ChildItem -LiteralPath $inputPath -Recurse -Force -File -Filter $pattern -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $hit) { throw "forbidden SBOM input: $($hit.Name)" }
}

$files = @()
$allFiles = Get-ChildItem -LiteralPath $inputPath -Recurse -Force -File |
    Where-Object { $_.FullName -ne $outputFullPath } |
    Sort-Object FullName
foreach ($file in $allFiles) {
    $relative = $file.FullName.Substring($inputPath.Length).TrimStart('\') -replace '\\', '/'
    $checksum = Get-Sha256 $file.FullName
    $fileId = 'SPDXRef-File-' + (($relative -replace '[^A-Za-z0-9.-]', '-') -replace '-+', '-')
    $files += [ordered]@{
        SPDXID = $fileId
        fileName = $relative
        checksums = @([ordered]@{ algorithm = 'SHA256'; checksumValue = $checksum })
        licenseConcluded = 'NOASSERTION'
        copyrightText = 'NOASSERTION'
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
    relationships = @([ordered]@{
        spdxElementId = 'SPDXRef-DOCUMENT'
        relationshipType = 'DESCRIBES'
        relatedSpdxElement = 'SPDXRef-Package-DeckPipe'
    })
}

Write-Utf8NoBom $outputFullPath ($sbom | ConvertTo-Json -Depth 10)
