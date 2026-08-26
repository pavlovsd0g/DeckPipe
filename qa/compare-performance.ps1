[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$BaselineJson,
    [Parameter(Mandatory)][string]$CurrentJson,
    [string]$OutputDirectory = '',
    [ValidateRange(0, 1000)][double]$MaxRegressionPercent = 25,
    [switch]$NoFailOnRegression
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot '.bench\runs'
}
Import-Module (Join-Path $PSScriptRoot 'DeckPipe.QA.psm1') -Force

function Get-PropertyValue {
    param([AllowNull()]$InputObject, [string]$Name, [AllowNull()]$Default = $null)
    if ($null -eq $InputObject) { return $Default }
    if ($InputObject -is [Collections.IDictionary]) {
        if ($InputObject.Contains($Name)) { return $InputObject[$Name] }
        return $Default
    }
    $property = $InputObject.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Get-Entries {
    param([AllowNull()]$InputObject)
    if ($null -eq $InputObject) { return @() }
    if ($InputObject -is [Collections.IDictionary]) {
        return @($InputObject.GetEnumerator() | ForEach-Object {
            [pscustomobject]@{ Name = [string]$_.Key; Value = $_.Value }
        })
    }
    return @($InputObject.PSObject.Properties | ForEach-Object {
        [pscustomobject]@{ Name = $_.Name; Value = $_.Value }
    })
}

function Test-NumericValue {
    param([AllowNull()]$Value)
    return $null -ne $Value -and $Value -isnot [bool] -and $Value -isnot [string] -and
        $Value -is [ValueType]
}

function Get-RunMetrics {
    param([Parameter(Mandatory)]$Run)

    $metrics = [ordered]@{}
    $performance = Get-PropertyValue -InputObject $Run -Name 'performance'
    foreach ($entry in Get-Entries -InputObject $performance) {
        if (Test-NumericValue -Value $entry.Value) {
            $metrics[$entry.Name] = [double]$entry.Value
        }
    }

    $startup = Get-PropertyValue -InputObject $Run -Name 'startup_ms'
    foreach ($mapping in @(
        [pscustomobject]@{ Source = 'port_ready'; Target = 'port_ready_ms' },
        [pscustomobject]@{ Source = 'window_ready'; Target = 'window_ready_ms' }
    )) {
        $value = Get-PropertyValue -InputObject $startup -Name $mapping.Source
        if (Test-NumericValue -Value $value -and -not $metrics.Contains($mapping.Target)) {
            $metrics[$mapping.Target] = [double]$value
        }
    }

    $footprint = Get-PropertyValue -InputObject $Run -Name 'process_footprint'
    foreach ($name in @('process_count', 'working_set_mb', 'private_memory_mb', 'idle_cpu_ms_over_2s')) {
        $value = Get-PropertyValue -InputObject $footprint -Name $name
        if (Test-NumericValue -Value $value -and -not $metrics.Contains($name)) {
            $metrics[$name] = [double]$value
        }
    }

    $apiMedian = Get-PropertyValue -InputObject $Run -Name 'api_median_ms'
    foreach ($entry in Get-Entries -InputObject $apiMedian) {
        $name = "api$($entry.Name) median_ms"
        if (Test-NumericValue -Value $entry.Value -and -not $metrics.Contains($name)) {
            $metrics[$name] = [double]$entry.Value
        }
    }

    $endpointMetrics = Get-PropertyValue -InputObject $Run -Name 'endpoint_metrics'
    foreach ($endpoint in Get-Entries -InputObject $endpointMetrics) {
        $median = Get-PropertyValue -InputObject $endpoint.Value -Name 'median_ms'
        $name = "api$($endpoint.Name) median_ms"
        if (Test-NumericValue -Value $median -and -not $metrics.Contains($name)) {
            $metrics[$name] = [double]$median
        }
    }
    return $metrics
}

foreach ($path in @($BaselineJson, $CurrentJson)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Performance input was not found: $path"
    }
}

$baseline = Get-Content -LiteralPath $BaselineJson -Raw | ConvertFrom-Json
$current = Get-Content -LiteralPath $CurrentJson -Raw | ConvertFrom-Json
$baselineMetrics = Get-RunMetrics -Run $baseline
$currentMetrics = Get-RunMetrics -Run $current
$comparisons = [Collections.Generic.List[object]]::new()
$checks = [Collections.Generic.List[object]]::new()

$index = 0
foreach ($name in $baselineMetrics.Keys) {
    if (-not $currentMetrics.Contains($name)) { continue }
    $index++
    $comparison = Compare-DeckPipeMetric -Name $name `
        -Baseline ([double]$baselineMetrics[$name]) `
        -Current ([double]$currentMetrics[$name]) `
        -MaxRegressionPercent $MaxRegressionPercent
    $comparisons.Add([pscustomobject][ordered]@{
        name = $comparison.Name
        baseline = $comparison.Baseline
        current = $comparison.Current
        delta = $comparison.Delta
        delta_percent = $comparison.DeltaPercent
        max_regression_percent = $comparison.MaxRegressionPercent
        status = $comparison.Status
    })
    $checkStatus = if ($comparison.Status -eq 'regression') { 'fail' } else { 'pass' }
    $checks.Add((New-DeckPipeCheck `
        -Id ('PERF-{0:D3}' -f $index) `
        -Status $checkStatus `
        -Message $(if ($checkStatus -eq 'pass') { "$name is within regression budget" } else { "$name exceeds regression budget" }) `
        -Data $null))
}

if ($comparisons.Count -eq 0) {
    throw 'Baseline and current run have no comparable numeric metrics.'
}

$failed = @($checks | Where-Object status -eq 'fail').Count
$passed = @($checks | Where-Object status -eq 'pass').Count
$baselineRunId = [string](Get-PropertyValue -InputObject $baseline -Name 'run_id' -Default 'baseline')
$currentRunId = [string](Get-PropertyValue -InputObject $current -Name 'run_id' -Default 'current')
$target = Get-PropertyValue -InputObject $current -Name 'target'
$targetProduct = [string](Get-PropertyValue -InputObject $target -Name 'product' -Default 'DeckPipe')
$targetVersion = [string](Get-PropertyValue -InputObject $target -Name 'version' -Default '')
$runId = 'performance-compare-' + [DateTimeOffset]::UtcNow.ToString('yyyyMMdd-HHmmssfff')

$run = [ordered]@{
    schema_version = 1
    run_id = $runId
    generated_at = [DateTimeOffset]::UtcNow.ToString('o')
    target = [ordered]@{ product = $targetProduct; version = $targetVersion; mode = 'performance-comparison' }
    baseline_run_id = $baselineRunId
    current_run_id = $currentRunId
    max_regression_percent = $MaxRegressionPercent
    summary = [ordered]@{
        status = if ($failed -gt 0) { 'fail' } else { 'pass' }
        passed = $passed
        failed = $failed
        warnings = 0
        skipped = 0
    }
    checks = $checks.ToArray()
    performance = $currentMetrics
    comparisons = $comparisons.ToArray()
}

$artifacts = Write-DeckPipeRunArtifacts -Run $run -OutputDirectory $OutputDirectory
Write-Host "PERF_COMPARE_RESULT status=$($run.summary.status) passed=$passed failed=$failed"
Write-Host "PERF_COMPARE_JSON $($artifacts.Json)"
Write-Host "PERF_COMPARE_MARKDOWN $($artifacts.Markdown)"

if ($failed -gt 0 -and -not $NoFailOnRegression) {
    exit 1
}
