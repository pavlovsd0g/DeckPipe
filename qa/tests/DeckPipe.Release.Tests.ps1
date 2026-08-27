$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path

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

function Read-JsonFile {
    param([string]$RelativePath)
    $path = Join-Path $repoRoot $RelativePath
    Assert-True (Test-Path -LiteralPath $path) "Missing $RelativePath"
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Read-TextFile {
    param([string]$RelativePath)
    $path = Join-Path $repoRoot $RelativePath
    Assert-True (Test-Path -LiteralPath $path) "Missing $RelativePath"
    return Get-Content -LiteralPath $path -Raw
}

function Get-LockRootVersion {
    param([string]$RelativePath)
    $text = Read-TextFile $RelativePath
    if ($text -notmatch '(?s)"packages"\s*:\s*\{\s*""\s*:\s*\{.*?"version"\s*:\s*"([^"]+)"') {
        throw "Cannot read root package version from $RelativePath"
    }
    return $Matches[1]
}

function Get-LockTopVersion {
    param([string]$RelativePath)
    $text = Read-TextFile $RelativePath
    if ($text -notmatch '(?m)^\s*"version"\s*:\s*"([^"]+)"\s*,') {
        throw "Cannot read top package-lock version from $RelativePath"
    }
    return $Matches[1]
}

It 'defines one canonical release version and synchronizes every package source' {
    $version = Read-JsonFile 'release\version.json'
    Assert-Equal $version.product 'DeckPipe'
    Assert-Equal $version.version '0.6.0'
    Assert-True ($version.build_id -match '^0\.6\.0\+[0-9]{8}\.[0-9]{6}\.[0-9a-f]{7,40}$') 'Build ID must include version, UTC timestamp, and git id'

    $rootPackage = Read-JsonFile 'package.json'
    $desktopPackage = Read-JsonFile 'desktop\package.json'
    $tauri = Read-JsonFile 'desktop\src-tauri\tauri.conf.json'

    Assert-Equal $rootPackage.version $version.version 'Root package version drift'
    Assert-Equal (Get-LockTopVersion 'package-lock.json') $version.version 'Root package-lock version drift'
    Assert-Equal (Get-LockRootVersion 'package-lock.json') $version.version 'Root package-lock root package drift'
    Assert-Equal $desktopPackage.version $version.version 'Desktop package version drift'
    Assert-Equal (Get-LockTopVersion 'desktop\package-lock.json') $version.version 'Desktop package-lock version drift'
    Assert-Equal (Get-LockRootVersion 'desktop\package-lock.json') $version.version 'Desktop package-lock root package drift'
    Assert-Equal $tauri.version $version.version 'Tauri config version drift'

    $cargoToml = Read-TextFile 'desktop\src-tauri\Cargo.toml'
    Assert-True ($cargoToml -match '(?m)^version = "0\.6\.0"$') 'Cargo.toml package version drift'
    Assert-False ((Read-TextFile 'app\main.py') -match 'APP_VERSION\s*=\s*"0\.[0-5]\.')
    Assert-False ((Read-TextFile 'app\bugreport.py') -match 'APP_VERSION\s*=\s*"0\.[0-5]\.')
}

It 'records Python runtime and build locks without fabricated hashes' {
    foreach ($relative in @('pyproject.toml', 'requirements.in', 'requirements.lock', 'requirements-build.lock')) {
        Assert-True (Test-Path -LiteralPath (Join-Path $repoRoot $relative)) "Missing $relative"
    }
    foreach ($relative in @('requirements.lock', 'requirements-build.lock')) {
        $text = Read-TextFile $relative
        Assert-True ($text -match '(?m)^# LOCK-STATUS: BLOCKED$') "$relative must explicitly block publication when hashes are unavailable"
        Assert-True ($text -match '(?m)^# BLOCKER: offline wheel cache does not contain all required distributions$') "$relative must document the offline hash blocker"
        Assert-True ($text -match '(?m)^[a-z0-9_.-]+==[0-9]') "$relative must still pin observed package versions"
        Assert-False ($text -match '(?i)PLACEHOLDER_HASH|FAKE_HASH|examplehash') "$relative contains placeholder hashes"
    }
}

It 'ships PowerShell 5.1 parseable release scripts without certificate-store discovery' {
    foreach ($relative in @('release\build.ps1', 'release\verify.ps1', 'release\New-SpdxSbom.ps1')) {
        $path = Join-Path $repoRoot $relative
        Assert-True (Test-Path -LiteralPath $path) "Missing $relative"
        $tokens = $null
        $parseErrors = $null
        [Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$parseErrors) | Out-Null
        Assert-Equal @($parseErrors).Count 0 "$relative parse errors"
        $text = Get-Content -LiteralPath $path -Raw
        Assert-False ($text -match '(?i)Get-ChildItem\s+Cert:|dir\s+Cert:|gci\s+Cert:|cert:\\') "$relative must not enumerate certificate stores"
        Assert-False ($text -match '(?i)SignTool\s+sign\s+/a\b') "$relative must not auto-select signing certificates"
    }
}

It 'fails closed on drift and forbidden release inputs before staging artifacts' {
    $build = Join-Path $repoRoot 'release\build.ps1'
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-release-test-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        $forbidden = Join-Path $temp 'config.local.json'
        [IO.File]::WriteAllText($forbidden, '{}', [Text.UTF8Encoding]::new($false))
        Assert-Throws { & $build -StagingDirectory $temp -UnsignedEngineeringCandidate } 'forbidden|drift|lock'
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

It 'blocks unsigned engineering candidate when Python hash locks are unavailable' {
    $build = Join-Path $repoRoot 'release\build.ps1'
    $verify = Join-Path $repoRoot 'release\verify.ps1'
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-release-build-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        Assert-Throws { & $build -StagingDirectory $temp -UnsignedEngineeringCandidate } 'Python lock BLOCKED'
        $verifyResult = & $verify -StagingDirectory $temp
        Assert-True (($verifyResult | Out-String) -match 'BLOCKED')
        Assert-False (Test-Path -LiteralPath (Join-Path $temp 'release-evidence.json')) 'Blocked build must not publish evidence'
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
