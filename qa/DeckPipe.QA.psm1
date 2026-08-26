Set-StrictMode -Version Latest

function Get-DeckPipeReadOnlyEndpoints {
    [CmdletBinding()]
    param()

    @(
        [pscustomobject]@{ Id = 'A2'; Method = 'GET'; Path = '/api/version'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'A3'; Method = 'GET'; Path = '/'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'A4'; Method = 'GET'; Path = '/api/config'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'B1'; Method = 'GET'; Path = '/api/playlists'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'B2'; Method = 'GET'; Path = '/api/sc/sources'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'B4'; Method = 'GET'; Path = '/api/rb/status'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'B5'; Method = 'GET'; Path = '/api/errors'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'D2'; Method = 'GET'; Path = '/api/jobs'; ExpectedStatus = 200 }
        [pscustomobject]@{ Id = 'A5'; Method = 'GET'; Path = '/__deckpipe_qa_missing__'; ExpectedStatus = 404 }
    )
}

function Assert-DeckPipeReadOnlyRequest {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Method,

        [Parameter(Mandatory)]
        [string]$Path
    )

    $normalizedMethod = $Method.Trim().ToUpperInvariant()
    $normalizedPath = $Path.Trim()
    $match = Get-DeckPipeReadOnlyEndpoints | Where-Object {
        $_.Method -ceq $normalizedMethod -and $_.Path -ceq $normalizedPath
    }

    if ($null -eq $match) {
        throw "Request '$normalizedMethod $normalizedPath' is not allowlisted for installed QA."
    }

    return $true
}

function Get-DeckPipeStringFlags {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [AllowEmptyString()]
        [string]$Value
    )

    if ($null -eq $Value) {
        $Value = ''
    }

    [pscustomobject]@{
        Length      = $Value.Length
        HasCyrillic = [bool]($Value -match '[\u0400-\u04FF]')
        HasBadGlyph = [bool]($Value -match '[\uFFFD\u25A0\u25A1\u25AF]')
    }
}

function Get-DeckPipeMetricSummary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [double[]]$Values
    )

    if ($Values.Count -eq 0) {
        throw 'At least one metric value is required.'
    }

    $ordered = @($Values | Sort-Object)
    $count = $ordered.Count
    if (($count % 2) -eq 0) {
        $median = ($ordered[($count / 2) - 1] + $ordered[$count / 2]) / 2.0
    } else {
        $median = $ordered[[math]::Floor($count / 2)]
    }

    $p95Index = [math]::Max(0, ([math]::Ceiling(0.95 * $count) - 1))

    [pscustomobject]@{
        Count  = $count
        Min    = [double]$ordered[0]
        Median = [double]$median
        P95    = [double]$ordered[$p95Index]
        Max    = [double]$ordered[$count - 1]
    }
}

function Compare-DeckPipeMetric {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Name,

        [Parameter(Mandatory)]
        [double]$Baseline,

        [Parameter(Mandatory)]
        [double]$Current,

        [Parameter(Mandatory)]
        [ValidateRange(0, [double]::MaxValue)]
        [double]$MaxRegressionPercent
    )

    $delta = $Current - $Baseline
    $deltaPercent = if ($Baseline -eq 0) {
        if ($Current -eq 0) { 0.0 } else { $null }
    } else {
        ($delta / [math]::Abs($Baseline)) * 100.0
    }

    $status = if ($null -eq $deltaPercent) {
        if ($Current -gt $Baseline) { 'regression' } else { 'within-budget' }
    } elseif ($deltaPercent -gt $MaxRegressionPercent) {
        'regression'
    } else {
        'within-budget'
    }

    [pscustomobject]@{
        Name                 = $Name
        Baseline             = $Baseline
        Current              = $Current
        Delta                = [math]::Round($delta, 3)
        DeltaPercent         = if ($null -eq $deltaPercent) { $null } else { [math]::Round($deltaPercent, 3) }
        MaxRegressionPercent = $MaxRegressionPercent
        Status               = $status
    }
}

function Get-DeckPipeFileSnapshot {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return [pscustomobject]@{
            Exists       = $false
            Bytes        = 0L
            Sha256       = $null
            LastWriteUtc = $null
        }
    }

    $item = Get-Item -LiteralPath $Path
    $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256
    [pscustomobject]@{
        Exists       = $true
        Bytes        = [long]$item.Length
        Sha256       = $hash.Hash.ToUpperInvariant()
        LastWriteUtc = $item.LastWriteTimeUtc.ToString('o')
    }
}

function Test-DeckPipeFileSnapshotEqual {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Before,

        [Parameter(Mandatory)]
        $After
    )

    return (
        [bool]$Before.Exists -eq [bool]$After.Exists -and
        [long]$Before.Bytes -eq [long]$After.Bytes -and
        [string]$Before.Sha256 -ceq [string]$After.Sha256 -and
        [string]$Before.LastWriteUtc -ceq [string]$After.LastWriteUtc
    )
}

function Get-DeckPipeDescendantProcessIds {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [int]$RootProcessId,

        [Parameter(Mandatory)]
        [object[]]$Processes
    )

    $result = [Collections.Generic.List[int]]::new()
    $seen = [Collections.Generic.HashSet[int]]::new()
    $queue = [Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($RootProcessId)

    while ($queue.Count -gt 0) {
        $current = $queue.Dequeue()
        if (-not $seen.Add($current)) {
            continue
        }
        $result.Add($current)
        foreach ($child in @($Processes | Where-Object { [int]$_.ParentProcessId -eq $current })) {
            $queue.Enqueue([int]$child.ProcessId)
        }
    }

    return $result.ToArray()
}

function Test-DeckPipeProcessPathOwned {
    [CmdletBinding()]
    param(
        [AllowNull()]
        [string]$ProcessPath,

        [Parameter(Mandatory)]
        [string]$InstallDirectory
    )

    if ([string]::IsNullOrWhiteSpace($ProcessPath)) {
        return $false
    }

    try {
        $candidate = [IO.Path]::GetFullPath($ProcessPath)
        $root = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd(
            [IO.Path]::DirectorySeparatorChar,
            [IO.Path]::AltDirectorySeparatorChar)
        $rootWithSeparator = $root + [IO.Path]::DirectorySeparatorChar
        return $candidate.StartsWith($rootWithSeparator, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Get-DeckPipeStringAggregate {
    param([AllowNull()]$InputObject)

    $state = [ordered]@{
        string_count = 0
        cyrillic_string_count = 0
        bad_glyph_string_count = 0
    }

    function Visit-DeckPipeValue {
        param([AllowNull()]$Value)

        if ($null -eq $Value) {
            return
        }
        if ($Value -is [string]) {
            $flags = Get-DeckPipeStringFlags -Value $Value
            $state.string_count++
            if ($flags.HasCyrillic) { $state.cyrillic_string_count++ }
            if ($flags.HasBadGlyph) { $state.bad_glyph_string_count++ }
            return
        }
        if ($Value -is [Collections.IDictionary]) {
            foreach ($entry in $Value.GetEnumerator()) {
                Visit-DeckPipeValue -Value $entry.Value
            }
            return
        }
        if ($Value -is [Collections.IEnumerable] -and $Value -isnot [string]) {
            foreach ($item in $Value) {
                Visit-DeckPipeValue -Value $item
            }
            return
        }
        if ($Value -is [psobject]) {
            foreach ($property in $Value.PSObject.Properties) {
                if ($property.MemberType -in @('NoteProperty', 'Property', 'AliasProperty', 'ScriptProperty')) {
                    Visit-DeckPipeValue -Value $property.Value
                }
            }
        }
    }

    Visit-DeckPipeValue -Value $InputObject
    return [pscustomobject]$state
}

function Get-DeckPipePropertyValue {
    param(
        [AllowNull()]$InputObject,
        [Parameter(Mandatory)][string]$Name,
        [AllowNull()]$Default = $null
    )

    if ($null -eq $InputObject) {
        return $Default
    }
    if ($InputObject -is [Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) {
            return $InputObject[$Name]
        }
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) {
        return $Default
    }
    return $property.Value
}

function Get-DeckPipeObjectNumericSum {
    param(
        [object[]]$Items,
        [Parameter(Mandatory)][string]$PropertyName
    )

    $sum = 0L
    foreach ($item in @($Items)) {
        $value = Get-DeckPipePropertyValue -InputObject $item -Name $PropertyName -Default 0
        if ($null -ne $value) {
            $sum += [long]$value
        }
    }
    return $sum
}

function Get-DeckPipePayloadSummary {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Endpoint,

        [AllowNull()]
        $Payload
    )

    switch ($Endpoint) {
        '/api/config' {
            $user = Get-DeckPipePropertyValue -InputObject $Payload -Name 'user'
            $userConfigured = $false
            if ($null -ne $user) {
                $userId = Get-DeckPipePropertyValue -InputObject $user -Name 'id'
                $userConfigured = -not [string]::IsNullOrWhiteSpace([string]$userId)
            }
            return [pscustomobject][ordered]@{
                kind                          = 'configuration-state'
                deezer_session_configured    = [bool](Get-DeckPipePropertyValue -InputObject $Payload -Name 'arl_set') -and $userConfigured
                soundcloud_session_configured = -not [string]::IsNullOrWhiteSpace([string](Get-DeckPipePropertyValue -InputObject $Payload -Name 'sc_user'))
                music_root_configured         = -not [string]::IsNullOrWhiteSpace([string](Get-DeckPipePropertyValue -InputObject $Payload -Name 'music_root'))
                wav_mode                      = [string](Get-DeckPipePropertyValue -InputObject $Payload -Name 'wav_mode')
                numbering                     = [bool](Get-DeckPipePropertyValue -InputObject $Payload -Name 'numbering')
            }
        }
        { $_ -in @('/api/playlists', '/api/sc/sources') } {
            [object[]]$items = @()
            if ($null -ne $Payload) { $items = @($Payload) }
            $strings = Get-DeckPipeStringAggregate -InputObject $items
            return [pscustomobject][ordered]@{
                kind                   = if ($Endpoint -eq '/api/playlists') { 'deezer-playlists' } else { 'soundcloud-sources' }
                item_count             = $items.Count
                declared_track_count   = Get-DeckPipeObjectNumericSum -Items $items -PropertyName 'count'
                ok_track_count         = Get-DeckPipeObjectNumericSum -Items $items -PropertyName 'ok'
                error_track_count      = Get-DeckPipeObjectNumericSum -Items $items -PropertyName 'errors'
                cyrillic_string_count  = $strings.cyrillic_string_count
                bad_glyph_string_count = $strings.bad_glyph_string_count
            }
        }
        '/api/rb/status' {
            $rawPlaylists = Get-DeckPipePropertyValue -InputObject $Payload -Name 'playlists' -Default $null
            [object[]]$playlists = @()
            if ($null -ne $rawPlaylists) { $playlists = @($rawPlaylists) }
            $strings = Get-DeckPipeStringAggregate -InputObject $playlists
            return [pscustomobject][ordered]@{
                kind                   = 'rekordbox-state'
                database_exists        = [bool](Get-DeckPipePropertyValue -InputObject $Payload -Name 'db_exists')
                rekordbox_running      = [bool](Get-DeckPipePropertyValue -InputObject $Payload -Name 'running')
                playlist_count         = $playlists.Count
                content_count          = Get-DeckPipeObjectNumericSum -Items $playlists -PropertyName 'count'
                has_read_error         = -not [string]::IsNullOrWhiteSpace([string](Get-DeckPipePropertyValue -InputObject $Payload -Name 'error'))
                cyrillic_string_count  = $strings.cyrillic_string_count
                bad_glyph_string_count = $strings.bad_glyph_string_count
            }
        }
        '/api/jobs' {
            [object[]]$jobs = @()
            if ($null -ne $Payload) { $jobs = @($Payload) }
            $active = @($jobs | Where-Object {
                [string](Get-DeckPipePropertyValue -InputObject $_ -Name 'status') -in @('queued', 'pending', 'running')
            })
            return [pscustomobject][ordered]@{
                kind             = 'jobs-state'
                job_count        = $jobs.Count
                active_job_count = $active.Count
            }
        }
        '/api/errors' {
            [object[]]$errors = @()
            if ($null -ne $Payload) { $errors = @($Payload) }
            $strings = Get-DeckPipeStringAggregate -InputObject $errors
            return [pscustomobject][ordered]@{
                kind                   = 'errors-state'
                error_count            = $errors.Count
                cyrillic_string_count  = $strings.cyrillic_string_count
                bad_glyph_string_count = $strings.bad_glyph_string_count
            }
        }
        '/api/version' {
            return [pscustomobject][ordered]@{
                kind            = 'version'
                reported_version = [string](Get-DeckPipePropertyValue -InputObject $Payload -Name 'version')
            }
        }
        default {
            $strings = Get-DeckPipeStringAggregate -InputObject $Payload
            return [pscustomobject][ordered]@{
                kind                   = 'generic'
                string_count           = $strings.string_count
                cyrillic_string_count  = $strings.cyrillic_string_count
                bad_glyph_string_count = $strings.bad_glyph_string_count
            }
        }
    }
}

function New-DeckPipeCheck {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [string]$Id,

        [Parameter(Mandatory)]
        [ValidateSet('pass', 'fail', 'warn', 'skipped')]
        [string]$Status,

        [Parameter(Mandatory)]
        [string]$Message,

        [AllowNull()]
        $Data = $null
    )

    [pscustomobject][ordered]@{
        id      = $Id
        status  = $Status
        message = $Message
        data    = $Data
    }
}

function Test-DeckPipeUiSnapshotReady {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]$Snapshot,
        [ValidateRange(1, [int]::MaxValue)]
        [int]$MinimumDescendants = 50
    )

    $descendants = [int](Get-DeckPipePropertyValue -InputObject $Snapshot -Name 'descendant_count' -Default 0)
    $required = [int](Get-DeckPipePropertyValue -InputObject $Snapshot -Name 'required_control_count' -Default 0)
    $missing = @(Get-DeckPipePropertyValue -InputObject $Snapshot -Name 'missing_control_ids' -Default @())
    return $descendants -ge $MinimumDescendants -and $required -gt 0 -and
        $missing.Count -eq 0
}

function Test-DeckPipeKeyboardReachable {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [object[]]$AncestorStates
    )

    foreach ($state in $AncestorStates) {
        $controlType = [string](Get-DeckPipePropertyValue -InputObject $state -Name 'control_type')
        if ($controlType -in @('Document', 'Window')) {
            return $false
        }
        if ([bool](Get-DeckPipePropertyValue -InputObject $state -Name 'focusable' -Default $false)) {
            return $true
        }
    }
    return $false
}

function ConvertTo-DeckPipeMarkdownCell {
    param([AllowNull()]$Value)

    if ($null -eq $Value) {
        return ''
    }

    return ([string]$Value).Replace('|', '\|').Replace("`r", ' ').Replace("`n", ' ')
}

function Write-DeckPipeAtomicText {
    param(
        [Parameter(Mandatory)]
        [string]$Path,

        [Parameter(Mandatory)]
        [string]$Content
    )

    $temporaryPath = "$Path.tmp-$([guid]::NewGuid().ToString('N'))"
    try {
        [IO.File]::WriteAllText($temporaryPath, $Content, [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($Path)) {
            [IO.File]::Replace($temporaryPath, $Path, $null, $true)
        } else {
            [IO.File]::Move($temporaryPath, $Path)
        }
    } finally {
        if ([IO.File]::Exists($temporaryPath)) {
            [IO.File]::Delete($temporaryPath)
        }
    }
}

function Write-DeckPipeRunArtifacts {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Run,

        [Parameter(Mandatory)]
        [string]$OutputDirectory
    )

    [IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null
    $runId = [string]$Run.run_id
    if ([string]::IsNullOrWhiteSpace($runId)) {
        throw 'Run must contain a non-empty run_id.'
    }

    $safeRunId = $runId -replace '[^A-Za-z0-9_.-]', '_'
    $jsonPath = Join-Path $OutputDirectory "$safeRunId.json"
    $markdownPath = Join-Path $OutputDirectory "$safeRunId.md"
    $json = ($Run | ConvertTo-Json -Depth 20) + "`n"

    $lines = [Collections.Generic.List[string]]::new()
    $lines.Add("# DeckPipe QA run ``$(ConvertTo-DeckPipeMarkdownCell $runId)``")
    $lines.Add('')
    $lines.Add("Generated: $(ConvertTo-DeckPipeMarkdownCell $Run.generated_at)")
    $lines.Add('')
    $lines.Add('## Summary')
    $lines.Add('')
    $lines.Add('| Status | Passed | Failed |')
    $lines.Add('|---|---:|---:|')
    $lines.Add("| $(ConvertTo-DeckPipeMarkdownCell $Run.summary.status) | $(ConvertTo-DeckPipeMarkdownCell $Run.summary.passed) | $(ConvertTo-DeckPipeMarkdownCell $Run.summary.failed) |")
    $lines.Add('')
    $lines.Add('## Checks')
    $lines.Add('')
    $lines.Add('| ID | Status | Message |')
    $lines.Add('|---|---|---|')
    foreach ($check in @($Run.checks)) {
        $lines.Add("| $(ConvertTo-DeckPipeMarkdownCell $check.id) | $(ConvertTo-DeckPipeMarkdownCell $check.status) | $(ConvertTo-DeckPipeMarkdownCell $check.message) |")
    }
    $lines.Add('')
    $lines.Add('## Performance')
    $lines.Add('')
    $lines.Add('| Metric | Value |')
    $lines.Add('|---|---:|')
    foreach ($property in $Run.performance.GetEnumerator()) {
        $lines.Add("| $(ConvertTo-DeckPipeMarkdownCell $property.Key) | $(ConvertTo-DeckPipeMarkdownCell $property.Value) |")
    }
    $lines.Add('')

    Write-DeckPipeAtomicText -Path $jsonPath -Content $json
    Write-DeckPipeAtomicText -Path $markdownPath -Content (($lines -join "`n") + "`n")

    [pscustomobject]@{
        Json     = $jsonPath
        Markdown = $markdownPath
    }
}

Export-ModuleMember -Function @(
    'Get-DeckPipeReadOnlyEndpoints',
    'Assert-DeckPipeReadOnlyRequest',
    'Get-DeckPipeStringFlags',
    'Get-DeckPipeMetricSummary',
    'Compare-DeckPipeMetric',
    'Get-DeckPipeFileSnapshot',
    'Test-DeckPipeFileSnapshotEqual',
    'Get-DeckPipeDescendantProcessIds',
    'Test-DeckPipeProcessPathOwned',
    'Get-DeckPipePayloadSummary',
    'New-DeckPipeCheck',
    'Test-DeckPipeUiSnapshotReady',
    'Test-DeckPipeKeyboardReachable',
    'Write-DeckPipeRunArtifacts'
)
