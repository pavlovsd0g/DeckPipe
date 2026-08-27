[CmdletBinding()]
param(
    [string]$ExePath = 'E:\DeckPipe\deckpipe.exe',
    [uri]$BaseUri = 'http://127.0.0.1:24680',
    [string]$OutputDirectory = '',
    [ValidateRange(1, 20)]
    [int]$Samples = 5,
    [ValidateRange(5, 60)]
    [int]$StartupTimeoutSeconds = 30,
    [switch]$NoFailOnFindings,
    [switch]$IsolatedUi,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$modulePath = Join-Path $PSScriptRoot 'DeckPipe.QA.psm1'
$budgetPath = Join-Path $PSScriptRoot 'performance-budget.json'
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

function Wait-DeckPipePort {
    param(
        [Parameter(Mandatory)][string]$HostName,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][int]$TimeoutSeconds
    )

    $watch = [Diagnostics.Stopwatch]::StartNew()
    while ($watch.Elapsed.TotalSeconds -lt $TimeoutSeconds) {
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $pending = $client.ConnectAsync($HostName, $Port)
            if ($pending.Wait(250) -and $client.Connected) {
                return [math]::Round($watch.Elapsed.TotalMilliseconds, 1)
            }
        } catch {
            # The sidecar is still starting.
        } finally {
            $client.Dispose()
        }
        Start-Sleep -Milliseconds 100
    }
    throw "DeckPipe port did not become ready within $TimeoutSeconds seconds."
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

if ($ValidateOnly) {
    foreach ($endpoint in $endpoints) {
        Assert-DeckPipeReadOnlyRequest -Method $endpoint.Method -Path $endpoint.Path | Out-Null
    }
    $validationMode = if ($IsolatedUi) { 'isolated-ui' } else { 'installed-read-only' }
    Write-Host "VALID installed-runner mode=$validationMode endpoints=$($endpoints.Count) samples=$Samples"
    return
}

if (-not (Test-Path -LiteralPath $ExePath -PathType Leaf)) {
    throw "DeckPipe executable was not found: $ExePath"
}
$resolvedExe = (Resolve-Path -LiteralPath $ExePath).Path
$installDirectory = Split-Path -Parent $resolvedExe

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
$userAppData = $env:APPDATA
$configPath = Join-Path $userAppData 'DeckPipe\config.local.json'
$databasePath = Join-Path $userAppData 'Pioneer\rekordbox\master.db'
$isolatedRoot = $null
$isolatedAppData = $null
$isolatedLocalAppData = $null
$isolatedConfigPath = $null
$isolatedConfigBefore = $null
$isolatedConfigUnchanged = $null
$isolationDirectoryRemoved = -not $IsolatedUi
$isolatedTabInteractions = 0
$configBefore = Get-DeckPipeFileSnapshot -Path $configPath
$databaseBefore = Get-DeckPipeFileSnapshot -Path $databasePath
$backupCountBefore = Get-DeckPipeBackupCount -DatabasePath $databasePath

function Add-RunCheck {
    param([string]$Id, [string]$Status, [string]$Message, [AllowNull()]$Data = $null)
    $checks.Add((New-DeckPipeCheck -Id $Id -Status $Status -Message $Message -Data $Data))
}

try {
    if ($IsolatedUi) {
        $isolatedRoot = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-isolated-qa-' + [guid]::NewGuid().ToString('N'))
        $isolatedAppData = Join-Path $isolatedRoot 'AppData\Roaming'
        $isolatedLocalAppData = Join-Path $isolatedRoot 'AppData\Local'
        $isolatedDeckPipeData = Join-Path $isolatedAppData 'DeckPipe'
        $isolatedMusic = Join-Path $isolatedRoot 'Music'
        foreach ($directory in @($isolatedDeckPipeData, $isolatedLocalAppData, $isolatedMusic)) {
            [IO.Directory]::CreateDirectory($directory) | Out-Null
        }
        $isolatedConfigPath = Join-Path $isolatedDeckPipeData 'config.local.json'
        $fixtureConfig = [ordered]@{
            music_root = $isolatedMusic
            wav_mode = 'source'
            numbering = $true
            sc_sources = @()
            local_sources = @()
        }
        [IO.File]::WriteAllText(
            $isolatedConfigPath,
            (($fixtureConfig | ConvertTo-Json -Depth 5) + "`n"),
            [Text.UTF8Encoding]::new($false))
        $isolatedConfigBefore = Get-DeckPipeFileSnapshot -Path $isolatedConfigPath
    }

    $inventory = Get-DeckPipeProcessInventory
    $alreadyRunning = @($inventory | Where-Object {
        Test-DeckPipeProcessPathOwned -ProcessPath $_.ExecutablePath -InstallDirectory $installDirectory
    })
    $portOwnerBefore = Get-DeckPipePortOwner -Port $BaseUri.Port
    if ($alreadyRunning.Count -gt 0 -or $null -ne $portOwnerBefore) {
        throw 'Installed QA preflight found an active DeckPipe process or occupied port.'
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
    $portReadyMs = Wait-DeckPipePort -HostName $BaseUri.Host -Port $BaseUri.Port -TimeoutSeconds $StartupTimeoutSeconds
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

    $httpHandler = [Net.Http.HttpClientHandler]::new()
    $httpHandler.UseProxy = $false
    $httpClient = [Net.Http.HttpClient]::new($httpHandler)
    $httpClient.Timeout = [TimeSpan]::FromSeconds(60)
    try {
        $results = @{}
        foreach ($endpoint in $endpoints) {
            $measurement = Invoke-DeckPipeMeasuredGet -Client $httpClient -Endpoint $endpoint -RootUri $BaseUri -SampleCount $Samples
            $requestLog.Add([pscustomobject]@{ method = 'GET'; path = $endpoint.Path })
            $unexpectedStatusCount = @($measurement.StatusCodes | Where-Object { $_ -ne $endpoint.ExpectedStatus }).Count
            $endpointMetrics[$endpoint.Path] = [ordered]@{
                expected_status = $endpoint.ExpectedStatus
                unexpected_status_count = $unexpectedStatusCount
                response_bytes = $measurement.ResponseBytes
                min_ms = $measurement.Metrics.Min
                median_ms = $measurement.Metrics.Median
                p95_ms = $measurement.Metrics.P95
                max_ms = $measurement.Metrics.Max
                payload = $measurement.Summary
            }
            $results[$endpoint.Path] = [pscustomobject]@{
                ExpectedStatus = $measurement.ExpectedStatus
                StatusCodes = $measurement.StatusCodes
                ResponseBytes = $measurement.ResponseBytes
                ContentType = $measurement.ContentType
                ParseOk = $measurement.ParseOk
                Metrics = $measurement.Metrics
                Summary = $measurement.Summary
            }
            if ($endpoint.Path -eq '/api/playlists') {
                $playlistTitles = @($measurement.Payload | ForEach-Object { [string](Get-PropertyValue -InputObject $_ -Name 'title') })
            }
            if ($endpoint.Path -eq '/api/config') {
                $configSensitiveFieldCount = Get-DeckPipeSensitiveFieldCount -InputObject $measurement.Payload
            }
        }

        $versionResult = $results['/api/version']
        $reportedVersion = [string]$versionResult.Summary.reported_version
        $versionPass = @($versionResult.StatusCodes | Where-Object { $_ -ne 200 }).Count -eq 0 -and
            $reportedVersion -eq [string]$budget.target_version
        Add-RunCheck -Id 'A2' -Status $(if ($versionPass) { 'pass' } else { 'fail' }) `
            -Message $(if ($versionPass) { 'installed version matches the QA target' } else { 'installed version differs from the QA target' }) `
            -Data ([ordered]@{ reported_version = $reportedVersion; expected_version = [string]$budget.target_version })

        $rootResult = $results['/']
        $rootPass = @($rootResult.StatusCodes | Where-Object { $_ -ne 200 }).Count -eq 0 -and
            $rootResult.ParseOk -and -not $rootResult.Summary.utf8_replacement_detected
        Add-RunCheck -Id 'A3' -Status $(if ($rootPass) { 'pass' } else { 'fail' }) `
            -Message $(if ($rootPass) { 'root document is readable UTF-8 HTML' } else { 'root document failed status or UTF-8 checks' }) `
            -Data ([ordered]@{ response_bytes = $rootResult.ResponseBytes; content_type = $rootResult.ContentType })

        $configResult = $results['/api/config']
        $configPass = @($configResult.StatusCodes | Where-Object { $_ -ne 200 }).Count -eq 0 -and
            $configResult.ParseOk -and $configSensitiveFieldCount -eq 0
        Add-RunCheck -Id 'A4' -Status $(if ($configPass) { 'pass' } else { 'fail' }) `
            -Message $(if ($configPass) { 'configuration endpoint exposes status fields only' } else { 'configuration endpoint exposed an unexpected sensitive field or invalid response' }) `
            -Data ([ordered]@{ unexpected_sensitive_field_count = $configSensitiveFieldCount; state = $configResult.Summary })

        $missingResult = $results['/__deckpipe_qa_missing__']
        $missingPass = @($missingResult.StatusCodes | Where-Object { $_ -ne 404 }).Count -eq 0
        Add-RunCheck -Id 'A5' -Status $(if ($missingPass) { 'pass' } else { 'fail' }) `
            -Message $(if ($missingPass) { 'unknown route returns 404' } else { 'unknown route did not consistently return 404' })

        foreach ($flow in @(
            [pscustomobject]@{ Id = 'B1'; Path = '/api/playlists'; Label = 'Deezer playlist listing' },
            [pscustomobject]@{ Id = 'B2'; Path = '/api/sc/sources'; Label = 'SoundCloud source listing' },
            [pscustomobject]@{ Id = 'B4'; Path = '/api/rb/status'; Label = 'Rekordbox read-only status' },
            [pscustomobject]@{ Id = 'B5'; Path = '/api/errors'; Label = 'error listing' }
        )) {
            $result = $results[$flow.Path]
            $pass = @($result.StatusCodes | Where-Object { $_ -ne 200 }).Count -eq 0 -and $result.ParseOk
            Add-RunCheck -Id $flow.Id -Status $(if ($pass) { 'pass' } else { 'fail' }) `
                -Message $(if ($pass) { "$($flow.Label) returned a parseable response" } else { "$($flow.Label) failed its response contract" }) `
                -Data $result.Summary
        }

        $apiBudgetFailures = [Collections.Generic.List[string]]::new()
        foreach ($endpoint in $endpoints) {
            $path = [string]$endpoint.Path
            $limitProperty = $budget.api_median_ms.PSObject.Properties[$path]
            if ($null -eq $limitProperty) { continue }
            $median = [double]$endpointMetrics[$path].median_ms
            $limit = [double]$limitProperty.Value
            if ($median -gt $limit) { $apiBudgetFailures.Add($path) }
            $performance["api$path median_ms"] = $median
        }
        Add-RunCheck -Id 'D2' -Status $(if ($apiBudgetFailures.Count -eq 0) { 'pass' } else { 'fail' }) `
            -Message $(if ($apiBudgetFailures.Count -eq 0) { 'all read-only endpoint medians are within budget' } else { "$($apiBudgetFailures.Count) endpoint medians exceed budget" }) `
            -Data ([ordered]@{ over_budget_count = $apiBudgetFailures.Count; over_budget_paths = $apiBudgetFailures.ToArray() })
    } finally {
        $httpClient.Dispose()
        $httpHandler.Dispose()
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
                if ($alive.Count -eq 0 -and $null -eq (Get-DeckPipePortOwner -Port $BaseUri.Port)) { break }
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
        } catch {
            if ($null -eq $fatalErrorType) { $fatalErrorType = $_.Exception.GetType().Name }
        }
    }

    $cleanupPortClosed = $null -eq (Get-DeckPipePortOwner -Port $BaseUri.Port)
    $lingeringOwned = @((Get-DeckPipeProcessInventory) | Where-Object {
        Test-DeckPipeProcessPathOwned -ProcessPath $_.ExecutablePath -InstallDirectory $installDirectory
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

    $passed = @($checks | Where-Object { $_.status -eq 'pass' }).Count
    $failed = @($checks | Where-Object { $_.status -eq 'fail' }).Count
    $warnings = @($checks | Where-Object { $_.status -eq 'warn' }).Count
    $skipped = @($checks | Where-Object { $_.status -eq 'skipped' }).Count
    $overallStatus = if ($failed -gt 0) { 'fail' } elseif ($warnings -gt 0) { 'warn' } else { 'pass' }
    $run = [ordered]@{
        schema_version = 1
        run_id = $runId
        generated_at = [DateTimeOffset]::UtcNow.ToString('o')
        target = [ordered]@{
            product = 'DeckPipe'
            version = $reportedVersion
            executable = [IO.Path]::GetFileName($resolvedExe)
            executable_sha256 = (Get-FileHash -LiteralPath $resolvedExe -Algorithm SHA256).Hash.ToUpperInvariant()
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
        summary = [ordered]@{
            status = $overallStatus
            passed = $passed
            failed = $failed
            warnings = $warnings
            skipped = $skipped
        }
        checks = $checks.ToArray()
        performance = $performance
        endpoint_metrics = $endpointMetrics
        fatal_error_type = $fatalErrorType
    }
    $artifacts = Write-DeckPipeRunArtifacts -Run $run -OutputDirectory $OutputDirectory
    Write-Host "QA_RESULT status=$overallStatus passed=$passed failed=$failed warnings=$warnings"
    Write-Host "QA_JSON $($artifacts.Json)"
    Write-Host "QA_MARKDOWN $($artifacts.Markdown)"
}

if (@($checks | Where-Object { $_.status -eq 'fail' }).Count -gt 0 -and -not $NoFailOnFindings) {
    exit 1
}
