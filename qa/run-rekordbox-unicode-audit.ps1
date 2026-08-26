[CmdletBinding()]
param(
    [string]$DatabasePath = (Join-Path $env:APPDATA 'Pioneer\rekordbox\master.db'),
    [string]$OutputDirectory = '',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot '.bench\runs'
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
}
$modulePath = Join-Path $PSScriptRoot 'DeckPipe.QA.psm1'
$auditPath = Join-Path $repoRoot 'audit\check_rekordbox_unicode.py'
Import-Module $modulePath -Force

foreach ($requiredPath in @($DatabasePath, $PythonPath, $auditPath)) {
    if (-not (Test-Path -LiteralPath $requiredPath -PathType Leaf)) {
        throw "Required file was not found: $requiredPath"
    }
}
if (@(Get-Process -Name rekordbox -ErrorAction SilentlyContinue).Count -gt 0) {
    throw 'Rekordbox must be closed before copying its database for QA.'
}

$source = (Resolve-Path -LiteralPath $DatabasePath).Path
$sourceBefore = Get-DeckPipeFileSnapshot -Path $source
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-rb-unicode-qa-' + [guid]::NewGuid().ToString('N'))
$copyPath = Join-Path $temporaryRoot 'master.db'
$stamp = [DateTimeOffset]::UtcNow.ToString('yyyyMMdd-HHmmssfff')
$outputPath = Join-Path $OutputDirectory "rekordbox-unicode-$stamp.json"
$auditSucceeded = $false

try {
    [IO.Directory]::CreateDirectory($temporaryRoot) | Out-Null
    Copy-Item -LiteralPath $source -Destination $copyPath
    $copySnapshot = Get-DeckPipeFileSnapshot -Path $copyPath
    if ($copySnapshot.Sha256 -cne $sourceBefore.Sha256 -or $copySnapshot.Bytes -ne $sourceBefore.Bytes) {
        throw 'The temporary Rekordbox copy does not match its source snapshot.'
    }

    & $PythonPath $auditPath $copyPath --output $outputPath
    if ($LASTEXITCODE -ne 0) {
        throw "Rekordbox Unicode audit exited with code $LASTEXITCODE."
    }
    $aggregate = Get-Content -LiteralPath $outputPath -Raw | ConvertFrom-Json
    foreach ($requiredProperty in @('content_count', 'title_cyrillic', 'title_bad_glyph', 'playlist_name_bad_glyph')) {
        if ($null -eq $aggregate.PSObject.Properties[$requiredProperty]) {
            throw "Unicode audit output is missing aggregate '$requiredProperty'."
        }
    }
    $auditSucceeded = $true
} finally {
    $sourceAfter = Get-DeckPipeFileSnapshot -Path $source
    $sourceUnchanged = Test-DeckPipeFileSnapshotEqual -Before $sourceBefore -After $sourceAfter

    if (Test-Path -LiteralPath $temporaryRoot -PathType Container) {
        $resolvedTemporaryRoot = [IO.Path]::GetFullPath($temporaryRoot)
        $resolvedTempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
        $safeTemporaryRoot = $resolvedTemporaryRoot.StartsWith(
            $resolvedTempBase, [StringComparison]::OrdinalIgnoreCase) -and
            [IO.Path]::GetFileName($resolvedTemporaryRoot).StartsWith(
                'deckpipe-rb-unicode-qa-', [StringComparison]::OrdinalIgnoreCase)
        if (-not $safeTemporaryRoot) {
            throw 'Refusing to remove an unexpected Unicode-audit directory.'
        }
        Remove-Item -LiteralPath $resolvedTemporaryRoot -Recurse -Force
    }

    if (-not $sourceUnchanged) {
        throw 'The source Rekordbox database changed during the copy audit.'
    }
}

if (-not $auditSucceeded) {
    throw 'Rekordbox Unicode audit did not complete.'
}
Write-Host "RB_UNICODE_RESULT source_unchanged=true temporary_removed=true"
Write-Host "RB_UNICODE_JSON $outputPath"
