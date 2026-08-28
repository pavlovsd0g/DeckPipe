$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$modulePath = Join-Path $repoRoot 'qa\DeckPipe.QA.psm1'
$installedRunnerPath = Join-Path $repoRoot 'qa\run-installed-qa.ps1'
$script:QaBuildId = '0.6.0+20260827.050713.6456dba254a6'
$script:TestNameFilter = [string]$env:DECKPIPE_QA_TEST_FILTER

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

function ConvertFrom-Utf8Base64 {
    param([string]$Value)
    return [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($Value))
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-TestSha256 {
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

function Get-TestCurrentHead {
    $revisionOutput = & git -C $repoRoot rev-parse HEAD 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $revisionOutput) { throw 'test setup failed: git rev-parse HEAD failed' }
    return [string](@($revisionOutput)[0])
}

function Update-InstalledQaTestStageInventory {
    param([string]$StagePath)

    $sbomFiles = @()
    $relationships = @([ordered]@{
        spdxElementId = 'SPDXRef-DOCUMENT'
        relationshipType = 'DESCRIBES'
        relatedSpdxElement = 'SPDXRef-Package-DeckPipe'
    })
    foreach ($file in @(Get-ChildItem -LiteralPath $StagePath -Force -File | Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'sbom.spdx.json') } | Sort-Object Name)) {
        $relative = $file.Name
        $sha256 = Get-TestSha256 $file.FullName
        $spdxId = 'SPDXRef-File-' + $sha256.Substring(0, 16)
        $sbomFiles += [ordered]@{
            SPDXID = $spdxId
            fileName = $relative
            checksums = @([ordered]@{ algorithm = 'SHA256'; checksumValue = $sha256 })
            licenseConcluded = 'NOASSERTION'
            copyrightText = 'NOASSERTION'
        }
        $relationships += [ordered]@{
            spdxElementId = 'SPDXRef-Package-DeckPipe'
            relationshipType = 'CONTAINS'
            relatedSpdxElement = $spdxId
        }
    }

    $sbom = [ordered]@{
        spdxVersion = 'SPDX-2.3'
        dataLicense = 'CC0-1.0'
        SPDXID = 'SPDXRef-DOCUMENT'
        name = "DeckPipe-$script:QaBuildId"
        documentNamespace = "https://deckpipe.local/spdx/$script:QaBuildId"
        creationInfo = [ordered]@{ created = '2026-08-27T05:07:13Z'; creators = @('Tool: qa-installed-identity-test') }
        packages = @([ordered]@{
            SPDXID = 'SPDXRef-Package-DeckPipe'
            name = 'DeckPipe'
            versionInfo = '0.6.0'
            downloadLocation = 'NOASSERTION'
            filesAnalyzed = $true
            licenseConcluded = 'NOASSERTION'
            licenseDeclared = 'NOASSERTION'
            copyrightText = 'NOASSERTION'
        })
        files = $sbomFiles
        relationships = $relationships
    }
    Write-Utf8NoBom (Join-Path $StagePath 'sbom.spdx.json') ($sbom | ConvertTo-Json -Depth 10)

    $manifestLines = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $StagePath -Force -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' } | Sort-Object Name)) {
        $manifestLines += "$(Get-TestSha256 $file.FullName)  $($file.Name)"
    }
    Write-Utf8NoBom (Join-Path $StagePath 'SHA256SUMS.txt') (($manifestLines -join "`n") + "`n")
}

function New-InstalledQaIdentityFixture {
    $artifactBytes = [Text.Encoding]::ASCII.GetBytes('prefix-__TAURI_BUNDLE_TYPE_VAR_UNK-suffix')
    $root = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-installed-identity-' + [guid]::NewGuid().ToString('N'))
    $stage = Join-Path $root 'stage'
    $install = Join-Path $root 'installed\DeckPipe'
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    [IO.Directory]::CreateDirectory($install) | Out-Null

    $sourceRevision = Get-TestCurrentHead
    $artifactName = "DeckPipe-$script:QaBuildId-$($sourceRevision.Substring(0, 7))-unsigned-private-beta-x64.exe"
    $artifactPath = Join-Path $stage $artifactName
    [IO.File]::WriteAllBytes($artifactPath, $artifactBytes)
    Copy-Item -LiteralPath (Join-Path $repoRoot 'release\policy.json') -Destination (Join-Path $stage 'policy.json') -Force

    $evidence = [ordered]@{
        schema_version = 1
        product = 'DeckPipe'
        version = '0.6.0'
        build_id = $script:QaBuildId
        source_revision = $sourceRevision
        artifacts = @([ordered]@{ path = $artifactName; type = 'exe'; sha256 = Get-TestSha256 $artifactPath })
        signing = [ordered]@{ status = 'WAIVED_BY_OWNER'; signed = @(); policy_path = 'policy.json' }
        timestamp = [ordered]@{ status = 'WAIVED_BY_OWNER'; policy_path = 'policy.json' }
        distribution = [ordered]@{
            channel = 'private-beta'
            artifact_label = 'unsigned-private-beta'
            policy_path = 'policy.json'
            policy_sha256 = Get-TestSha256 (Join-Path $stage 'policy.json')
        }
    }
    Write-Utf8NoBom (Join-Path $stage 'release-evidence.json') ($evidence | ConvertTo-Json -Depth 10)
    Update-InstalledQaTestStageInventory -StagePath $stage

    $installedPath = Join-Path $install 'DeckPipe.exe'
    [IO.File]::WriteAllBytes($installedPath, $artifactBytes)
    return [pscustomobject]@{
        Root = $root
        Stage = $stage
        Artifact = $artifactPath
        ArtifactName = $artifactName
        Installed = $installedPath
    }
}

function Set-InstalledQaBundleMarker {
    param([Parameter(Mandatory)][string]$Path)

    $bytes = [IO.File]::ReadAllBytes($Path)
    $old = [Text.Encoding]::ASCII.GetBytes('__TAURI_BUNDLE_TYPE_VAR_UNK')
    $new = [Text.Encoding]::ASCII.GetBytes('__TAURI_BUNDLE_TYPE_VAR_NSS')
    $offset = -1
    for ($index = 0; $index -le ($bytes.Length - $old.Length); $index++) {
        $matched = $true
        for ($inner = 0; $inner -lt $old.Length; $inner++) {
            if ($bytes[$index + $inner] -ne $old[$inner]) {
                $matched = $false
                break
            }
        }
        if ($matched) {
            $offset = $index
            break
        }
    }
    if ($offset -lt 0) { throw 'test setup failed: staged marker not found' }
    for ($inner = 0; $inner -lt $new.Length; $inner++) {
        $bytes[$offset + $inner] = $new[$inner]
    }
    [IO.File]::WriteAllBytes($Path, $bytes)
}

function Invoke-InstalledQaIdentitySelfTest {
    param([string]$ExePath, [string]$CandidateEvidenceDirectory)

    $quotedRunner = "'" + $installedRunnerPath.Replace("'", "''") + "'"
    $quotedExe = "'" + $ExePath.Replace("'", "''") + "'"
    $quotedEvidence = "'" + $CandidateEvidenceDirectory.Replace("'", "''") + "'"
    $command = @"
& $quotedRunner -SelfTestContract identity -ExePath $quotedExe -CandidateEvidenceDirectory $quotedEvidence
exit `$LASTEXITCODE
"@
    $encodedCommand = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = 'powershell.exe'
    $startInfo.Arguments = "-NoProfile -ExecutionPolicy Bypass -EncodedCommand $encodedCommand"
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = [Diagnostics.Process]::Start($startInfo)
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(30000)) {
        $process.Kill()
        $process.WaitForExit()
        return [pscustomobject]@{
            ExitCode = 124
            Output = 'identity self-test timed out after 30 seconds'
        }
    }
    $process.WaitForExit()
    $stdoutTask.Wait(5000) | Out-Null
    $stderrTask.Wait(5000) | Out-Null
    return [pscustomobject]@{
        ExitCode = $process.ExitCode
        Output = ([string]$stdoutTask.Result + [string]$stderrTask.Result)
    }
}

function ConvertFrom-InstalledQaSelfTestJson {
    param([string]$Output)
    if ($Output -notmatch 'SELFTEST_JSON (?<json>\{.+\})') {
        throw "SELFTEST_JSON was not emitted. Output: $Output"
    }
    return $Matches['json'] | ConvertFrom-Json
}

function It {
    param([string]$Name, [scriptblock]$Body)
    if (-not [string]::IsNullOrWhiteSpace($script:TestNameFilter) -and $Name -notmatch $script:TestNameFilter) {
        return
    }
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
    $clean = Get-DeckPipeStringFlags -Value (ConvertFrom-Utf8Base64 '0KHQvtCy0LXRgtGB0LrQuNC5INGB0LjQvdGC0LXQt9Cw0YLQvtGAIOKAlCDRkdC20LjQug==')
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

It 'accepts a private-beta NSIS install when only the Tauri bundle marker code changed' {
    $fixture = New-InstalledQaIdentityFixture
    try {
        Set-InstalledQaBundleMarker -Path $fixture.Installed
        $result = Invoke-InstalledQaIdentitySelfTest -ExePath $fixture.Installed -CandidateEvidenceDirectory $fixture.Stage
        Assert-Equal $result.ExitCode 0 "Expected identity self-test to accept Tauri NSIS marker drift. Output: $($result.Output)"
        $payload = ConvertFrom-InstalledQaSelfTestJson -Output $result.Output
        Assert-True $payload.identity.matched
        Assert-True $payload.launch_allowed
        Assert-Equal $payload.identity.identity_match_mode 'tauri-bundle-marker'
        Assert-Equal $payload.identity.accepted_bundle_type 'NSS'
        Assert-Equal $payload.identity.staged_artifact $fixture.Artifact
        Assert-Equal $payload.identity.staged_artifact_path $fixture.ArtifactName
        Assert-Equal $payload.identity.actual_sha256 (Get-TestSha256 $fixture.Installed)
        Assert-False ([string]$payload.identity.actual_sha256 -eq [string]$payload.identity.staged_sha256) 'Marker acceptance must retain the distinct installed hash'
    } finally {
        if (Test-Path -LiteralPath $fixture.Root -PathType Container) { [IO.Directory]::Delete($fixture.Root, $true) }
    }
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
