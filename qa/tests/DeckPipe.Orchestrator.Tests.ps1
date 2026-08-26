$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$modulePath = Join-Path $repoRoot 'qa\DeckPipe.QA.psm1'
Import-Module $modulePath -Force

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

It 'walks only the process tree rooted at the launched PID' {
    $processes = @(
        [pscustomobject]@{ ProcessId = 10; ParentProcessId = 1; ExecutablePath = 'E:\DeckPipe\deckpipe.exe' },
        [pscustomobject]@{ ProcessId = 11; ParentProcessId = 10; ExecutablePath = 'E:\DeckPipe\deckpipe-backend.exe' },
        [pscustomobject]@{ ProcessId = 12; ParentProcessId = 11; ExecutablePath = 'C:\Windows\System32\conhost.exe' },
        [pscustomobject]@{ ProcessId = 20; ParentProcessId = 1; ExecutablePath = 'E:\DeckPipe\deckpipe.exe' }
    )
    $ids = @(Get-DeckPipeDescendantProcessIds -RootProcessId 10 -Processes $processes)
    Assert-Equal ($ids -join ',') '10,11,12'
}

It 'recognizes install-owned executables without accepting a prefix collision' {
    Assert-True (Test-DeckPipeProcessPathOwned -ProcessPath 'E:\DeckPipe\deckpipe.exe' -InstallDirectory 'E:\DeckPipe')
    Assert-True (Test-DeckPipeProcessPathOwned -ProcessPath 'e:\deckpipe\DECKPIPE-BACKEND.EXE' -InstallDirectory 'E:\DeckPipe')
    Assert-False (Test-DeckPipeProcessPathOwned -ProcessPath 'E:\DeckPipe-Evil\deckpipe.exe' -InstallDirectory 'E:\DeckPipe')
    Assert-False (Test-DeckPipeProcessPathOwned -ProcessPath $null -InstallDirectory 'E:\DeckPipe')
}

It 'summarizes configuration state without retaining identity or filesystem values' {
    $payload = [pscustomobject]@{
        music_root = 'C:\Sensitive\Music'
        arl_set = $true
        wav_mode = 'wav'
        numbering = $true
        sc_user = 'секретный-пользователь'
        user = [pscustomobject]@{ id = '123'; email = 'private@example.invalid' }
    }
    $summary = Get-DeckPipePayloadSummary -Endpoint '/api/config' -Payload $payload
    Assert-True $summary.deezer_session_configured
    Assert-True $summary.soundcloud_session_configured
    Assert-True $summary.music_root_configured
    Assert-Equal $summary.wav_mode 'wav'
    $serialized = $summary | ConvertTo-Json -Compress
    Assert-False ($serialized -match 'Sensitive|секретный|private@example|123')
}

It 'counts Unicode and replacement glyphs without retaining playlist titles' {
    $payload = @(
        [pscustomobject]@{ id = '1'; title = 'Советский синтезатор'; count = 3; ok = 2; errors = 0; path = 'C:\Private\One' },
        [pscustomobject]@{ id = '2'; title = ('Broken ' + [char]0xFFFD); count = 4; ok = 1; errors = 1; path = 'C:\Private\Two' }
    )
    $summary = Get-DeckPipePayloadSummary -Endpoint '/api/playlists' -Payload $payload
    Assert-Equal $summary.item_count 2
    Assert-Equal $summary.cyrillic_string_count 1
    Assert-Equal $summary.bad_glyph_string_count 1
    Assert-Equal $summary.declared_track_count 7
    Assert-Equal $summary.ok_track_count 3
    Assert-Equal $summary.error_track_count 1
    $serialized = $summary | ConvertTo-Json -Compress
    Assert-False ($serialized -match 'Советский|Broken|Private')
}

It 'treats an empty JSON collection as zero aggregate counts' {
    $summary = Get-DeckPipePayloadSummary -Endpoint '/api/playlists' -Payload $null
    Assert-Equal $summary.item_count 0
    Assert-Equal $summary.declared_track_count 0
    Assert-Equal $summary.ok_track_count 0
    Assert-Equal $summary.error_track_count 0
}

It 'summarizes Rekordbox and jobs responses as aggregate state only' {
    $rb = Get-DeckPipePayloadSummary -Endpoint '/api/rb/status' -Payload ([pscustomobject]@{
        db_exists = $true
        running = $false
        playlists = @(
            [pscustomobject]@{ id = 'a'; name = 'Русский плейлист'; count = 8 },
            [pscustomobject]@{ id = 'b'; name = 'Second'; count = 4 }
        )
    })
    Assert-True $rb.database_exists
    Assert-False $rb.rekordbox_running
    Assert-Equal $rb.playlist_count 2
    Assert-Equal $rb.content_count 12
    Assert-Equal $rb.cyrillic_string_count 1

    $jobs = Get-DeckPipePayloadSummary -Endpoint '/api/jobs' -Payload @(
        [pscustomobject]@{ id = 'one'; status = 'done' },
        [pscustomobject]@{ id = 'two'; status = 'running' }
    )
    Assert-Equal $jobs.job_count 2
    Assert-Equal $jobs.active_job_count 1
}

It 'builds a stable check result and rejects unsafe status values' {
    $check = New-DeckPipeCheck -Id 'A1' -Status 'pass' -Message 'window ready' -Data @{ elapsed_ms = 1200 }
    Assert-Equal $check.id 'A1'
    Assert-Equal $check.status 'pass'
    Assert-Equal $check.data.elapsed_ms 1200
    try {
        New-DeckPipeCheck -Id 'A1' -Status 'maybe' -Message 'bad' | Out-Null
        throw 'Expected invalid status to be rejected'
    } catch {
        Assert-True ($_.Exception.Message -match 'status')
    }
}

It 'ships a parseable installed runner and machine-readable performance budget' {
    $runnerPath = Join-Path $repoRoot 'qa\run-installed-qa.ps1'
    $budgetPath = Join-Path $repoRoot 'qa\performance-budget.json'
    Assert-True (Test-Path -LiteralPath $runnerPath) "Missing runner $runnerPath"
    Assert-True (Test-Path -LiteralPath $budgetPath) "Missing budget $budgetPath"

    $tokens = $null
    $parseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$parseErrors) | Out-Null
    Assert-Equal @($parseErrors).Count 0

    $budget = Get-Content -LiteralPath $budgetPath -Raw | ConvertFrom-Json
    Assert-True ($budget.window_ready_ms -gt 0)
    Assert-True ($budget.working_set_mb -gt 0)
    Assert-True ($budget.api_median_ms.'/api/rb/status' -gt 0)

    $runnerText = Get-Content -LiteralPath $runnerPath -Raw
    Assert-True ($runnerText -match 'Assert-DeckPipeReadOnlyRequest')
    Assert-False ($runnerText -match '(?i)-Method\s+[''"]?(POST|PUT|PATCH|DELETE)')
}

It 'rejects a transient accessibility tree until required controls are present' {
    $transient = [pscustomobject]@{
        descendant_count = 17
        required_control_count = 6
        required_visible_count = 0
        missing_control_ids = @('save')
    }
    $ready = [pscustomobject]@{
        descendant_count = 190
        required_control_count = 6
        required_visible_count = 5
        missing_control_ids = @()
    }
    Assert-False (Test-DeckPipeUiSnapshotReady -Snapshot $transient -MinimumDescendants 50)
    Assert-True (Test-DeckPipeUiSnapshotReady -Snapshot $ready -MinimumDescendants 50)
}

It 'does not treat the focusable document root as keyboard access to a playlist card' {
    $onlyDocumentFocusable = @(
        [pscustomobject]@{ control_type = 'Text'; focusable = $false },
        [pscustomobject]@{ control_type = 'Group'; focusable = $false },
        [pscustomobject]@{ control_type = 'Document'; focusable = $true }
    )
    $actionableGroup = @(
        [pscustomobject]@{ control_type = 'Text'; focusable = $false },
        [pscustomobject]@{ control_type = 'Group'; focusable = $true },
        [pscustomobject]@{ control_type = 'Document'; focusable = $true }
    )
    Assert-False (Test-DeckPipeKeyboardReachable -AncestorStates $onlyDocumentFocusable)
    Assert-True (Test-DeckPipeKeyboardReachable -AncestorStates $actionableGroup)
}

It 'ships a parseable read-only Rekordbox Unicode audit runner' {
    $runnerPath = Join-Path $repoRoot 'qa\run-rekordbox-unicode-audit.ps1'
    Assert-True (Test-Path -LiteralPath $runnerPath) "Missing runner $runnerPath"
    $tokens = $null
    $parseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile($runnerPath, [ref]$tokens, [ref]$parseErrors) | Out-Null
    Assert-Equal @($parseErrors).Count 0
    $runnerText = Get-Content -LiteralPath $runnerPath -Raw
    Assert-True ($runnerText -match 'Get-DeckPipeFileSnapshot')
    Assert-True ($runnerText -match 'Copy-Item')
    Assert-False ($runnerText -match '(?i)master\.db.+(Set-Content|WriteAllText)')
}

It 'writes a machine-readable performance comparison with regression status' {
    $runnerPath = Join-Path $repoRoot 'qa\compare-performance.ps1'
    Assert-True (Test-Path -LiteralPath $runnerPath) "Missing runner $runnerPath"
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-compare-qa-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        $baselinePath = Join-Path $temp 'baseline.json'
        $currentPath = Join-Path $temp 'current.json'
        $baseline = [ordered]@{
            run_id = 'baseline-fixture'
            target = [ordered]@{ product = 'DeckPipe'; version = '0.5.0' }
            performance = [ordered]@{ window_ready_ms = 100.0; working_set_mb = 500.0 }
            endpoint_metrics = [ordered]@{ '/api/config' = [ordered]@{ median_ms = 100.0 } }
        }
        $current = [ordered]@{
            run_id = 'current-fixture'
            target = [ordered]@{ product = 'DeckPipe'; version = '0.5.1' }
            performance = [ordered]@{ window_ready_ms = 110.0; working_set_mb = 510.0 }
            endpoint_metrics = [ordered]@{ '/api/config' = [ordered]@{ median_ms = 131.0 } }
        }
        [IO.File]::WriteAllText($baselinePath, ($baseline | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText($currentPath, ($current | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
        & $runnerPath -BaselineJson $baselinePath -CurrentJson $currentPath -OutputDirectory $temp -NoFailOnRegression | Out-Null
        $comparisonPath = Get-ChildItem -LiteralPath $temp -Filter 'performance-compare-*.json' | Select-Object -First 1
        Assert-True ($null -ne $comparisonPath) 'Comparison JSON was not created'
        $comparison = Get-Content -LiteralPath $comparisonPath.FullName -Raw | ConvertFrom-Json
        Assert-Equal $comparison.summary.failed 1
        $apiResult = $comparison.comparisons | Where-Object name -eq 'api/api/config median_ms'
        Assert-Equal $apiResult.status 'regression'
        Assert-Equal $apiResult.delta_percent 31.0
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
