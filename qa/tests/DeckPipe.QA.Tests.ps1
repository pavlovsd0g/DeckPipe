$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$modulePath = Join-Path $repoRoot 'qa\DeckPipe.QA.psm1'

function Assert-True {
    param([bool]$Condition, [string]$Message = 'Expected condition to be true')
    if (-not $Condition) { throw $Message }
}

function Assert-False {
    param([bool]$Condition, [string]$Message = 'Expected condition to be false')
    if ($Condition) { throw $Message }
}

function Assert-Equal {
    param($Actual, $Expected, [string]$Message = '')
    if ($Actual -ne $Expected) {
        $prefix = if ($Message) { "$Message. " } else { '' }
        throw "${prefix}Expected '$Expected', got '$Actual'"
    }
}

function Assert-Throws {
    param([scriptblock]$Body, [string]$MessagePattern)
    try {
        & $Body
    } catch {
        if ($_.Exception.Message -notmatch $MessagePattern) {
            throw "Expected error matching '$MessagePattern', got '$($_.Exception.Message)'"
        }
        return
    }
    throw "Expected an exception matching '$MessagePattern'"
}

function It {
    param([string]$Name, [scriptblock]$Body)
    try {
        & $Body
        $script:Passed++
        Write-Host "PASS $Name"
    } catch {
        $script:Failed++
        Write-Host "FAIL $Name :: $($_.Exception.Message)"
    }
}

It 'provides the QA module' {
    Assert-True (Test-Path -LiteralPath $modulePath) "Missing module $modulePath"
}

if (Test-Path -LiteralPath $modulePath) {
    Import-Module $modulePath -Force
}

It 'allows only the explicit read-only GET surface' {
    $allowed = @(Get-DeckPipeReadOnlyEndpoints)
    Assert-Equal $allowed.Count 9
    Assert-True ($allowed.Path -contains '/api/version')
    Assert-True ($allowed.Path -contains '/api/playlists')
    Assert-True ($allowed.Path -contains '/__deckpipe_qa_missing__')
    Assert-False ($allowed.Path -contains '/api/playlists/123/tracks')
    foreach ($endpoint in $allowed) {
        Assert-Equal $endpoint.Method 'GET' "Unsafe method for $($endpoint.Path)"
    }
}

It 'rejects mutation and track-scan paths before a request is made' {
    Assert-Throws { Assert-DeckPipeReadOnlyRequest -Method POST -Path '/api/config' } 'not allowlisted'
    Assert-Throws { Assert-DeckPipeReadOnlyRequest -Method GET -Path '/api/playlists/123/tracks' } 'not allowlisted'
    Assert-Throws { Assert-DeckPipeReadOnlyRequest -Method GET -Path '/api/sc/sync-account' } 'not allowlisted'
    Assert-True (Assert-DeckPipeReadOnlyRequest -Method GET -Path '/api/config')
}

It 'detects Cyrillic and replacement glyphs without returning source text' {
    $clean = Get-DeckPipeStringFlags -Value 'Советский синтезатор — ёжик'
    Assert-True $clean.HasCyrillic
    Assert-False $clean.HasBadGlyph
    Assert-False ($clean.PSObject.Properties.Name -contains 'Value')

    $bad = Get-DeckPipeStringFlags -Value ("title " + [char]0xFFFD)
    Assert-True $bad.HasBadGlyph
}

It 'calculates deterministic latency statistics' {
    $stats = Get-DeckPipeMetricSummary -Values @(1.0, 2.0, 3.0, 100.0)
    Assert-Equal $stats.Count 4
    Assert-Equal $stats.Min 1.0
    Assert-Equal $stats.Median 2.5
    Assert-Equal $stats.P95 100.0
    Assert-Equal $stats.Max 100.0
}

It 'classifies a lower-is-better regression against the allowed budget' {
    $result = Compare-DeckPipeMetric -Name 'api.config.median_ms' -Baseline 100 -Current 131 -MaxRegressionPercent 25
    Assert-Equal $result.Status 'regression'
    Assert-Equal $result.Delta 31.0
    Assert-Equal $result.DeltaPercent 31.0

    $within = Compare-DeckPipeMetric -Name 'api.config.median_ms' -Baseline 100 -Current 124 -MaxRegressionPercent 25
    Assert-Equal $within.Status 'within-budget'
}

It 'captures file identity without exposing file contents' {
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-qa-snapshot-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $secretFile = Join-Path $temp 'config.local.json'
    [IO.File]::WriteAllText($secretFile, '{"arl":"DO_NOT_LEAK_THIS_VALUE"}', [Text.UTF8Encoding]::new($false))
    try {
        $snapshot = Get-DeckPipeFileSnapshot -Path $secretFile
        Assert-True $snapshot.Exists
        Assert-Equal $snapshot.Bytes 32
        Assert-Equal $snapshot.Sha256.Length 64
        Assert-False (($snapshot | ConvertTo-Json -Compress) -match 'DO_NOT_LEAK')
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

It 'detects a safety snapshot mutation' {
    $before = [pscustomobject]@{ Exists = $true; Bytes = 10L; Sha256 = 'AAA'; LastWriteUtc = '2026-01-01T00:00:00Z' }
    $same = [pscustomobject]@{ Exists = $true; Bytes = 10L; Sha256 = 'AAA'; LastWriteUtc = '2026-01-01T00:00:00Z' }
    $changed = [pscustomobject]@{ Exists = $true; Bytes = 11L; Sha256 = 'BBB'; LastWriteUtc = '2026-01-01T00:00:01Z' }
    Assert-True (Test-DeckPipeFileSnapshotEqual -Before $before -After $same)
    Assert-False (Test-DeckPipeFileSnapshotEqual -Before $before -After $changed)
}

It 'writes redacted JSON and Markdown run artifacts atomically' {
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-qa-artifacts-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        $run = [ordered]@{
            schema_version = 1
            run_id = 'fixture-run'
            generated_at = '2026-08-26T00:00:00Z'
            target = [ordered]@{ product = 'DeckPipe'; version = '0.5.0' }
            summary = [ordered]@{ status = 'fail'; passed = 2; failed = 1 }
            checks = @(
                [ordered]@{ id = 'A1'; status = 'pass'; message = 'window ready' },
                [ordered]@{ id = 'C2'; status = 'fail'; message = 'controls clipped' }
            )
            performance = [ordered]@{ window_ready_ms = 1234; working_set_mb = 500 }
        }
        $paths = Write-DeckPipeRunArtifacts -Run $run -OutputDirectory $temp
        Assert-True (Test-Path -LiteralPath $paths.Json)
        Assert-True (Test-Path -LiteralPath $paths.Markdown)
        $loaded = Get-Content -LiteralPath $paths.Json -Raw | ConvertFrom-Json
        Assert-Equal $loaded.run_id 'fixture-run'
        $markdown = Get-Content -LiteralPath $paths.Markdown -Raw
        Assert-True ($markdown -match 'C2')
        Assert-True ($markdown -match 'controls clipped')
        Assert-False ($markdown -match 'token|oauth|arl')
        Assert-False (Test-Path -LiteralPath ($paths.Json + '.tmp'))
        Assert-False (Test-Path -LiteralPath ($paths.Markdown + '.tmp'))
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
