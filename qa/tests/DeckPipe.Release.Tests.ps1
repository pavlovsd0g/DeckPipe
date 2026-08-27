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

function Invoke-ReleaseScript {
    param([string]$RelativePath, [string[]]$Arguments = @())
    $scriptPath = Join-Path $repoRoot $RelativePath
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $scriptPath @Arguments 2>&1
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Output = ($output | Out-String)
    }
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

function New-SyntheticReleaseStage {
    param(
        [switch]$WithExecutable,
        [switch]$SignedEvidence,
        [string]$StageName = 'deckpipe-release-stage'
    )
    $temp = Join-Path ([IO.Path]::GetTempPath()) ($StageName + '-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $files = @()
    $evidence = [ordered]@{
        schema_version = 1
        product = 'DeckPipe'
        version = '0.6.0'
        build_id = '0.6.0+20260827.050713.6456dba254a6'
        source_revision = '09767c457f1578412c500cc9da13472f9fb2412c'
        artifacts = @()
        signing = [ordered]@{ status = if ($SignedEvidence) { 'PASS' } else { 'BLOCKED' }; signed = @() }
        timestamp = [ordered]@{ status = if ($SignedEvidence) { 'PASS' } else { 'BLOCKED' } }
    }
    if ($WithExecutable) {
        $artifactName = 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-09767c4-x64-setup.exe'
        $artifactPath = Join-Path $temp $artifactName
        Write-Utf8NoBom $artifactPath 'synthetic installer bytes'
        $hash = Get-TestSha256 $artifactPath
        $evidence.artifacts += [ordered]@{ path = $artifactName; type = 'installer'; sha256 = $hash }
        $evidence.signing.signed += $artifactName
        $files += $artifactName
    }
    $evidencePath = Join-Path $temp 'release-evidence.json'
    Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 8)
    $files += 'release-evidence.json'

    $sbomFiles = @()
    foreach ($relative in $files | Sort-Object) {
        $full = Join-Path $temp $relative
        $spdxId = 'SPDXRef-File-' + (Get-TestSha256 $full).Substring(0, 16)
        $sbomFiles += [ordered]@{
            SPDXID = $spdxId
            fileName = $relative
            checksums = @([ordered]@{ algorithm = 'SHA256'; checksumValue = Get-TestSha256 $full })
            licenseConcluded = 'NOASSERTION'
            copyrightText = 'NOASSERTION'
        }
    }
    $relationships = @([ordered]@{
        spdxElementId = 'SPDXRef-DOCUMENT'
        relationshipType = 'DESCRIBES'
        relatedSpdxElement = 'SPDXRef-Package-DeckPipe'
    })
    foreach ($file in $sbomFiles) {
        $relationships += [ordered]@{
            spdxElementId = 'SPDXRef-Package-DeckPipe'
            relationshipType = 'CONTAINS'
            relatedSpdxElement = $file.SPDXID
        }
    }
    $sbom = [ordered]@{
        spdxVersion = 'SPDX-2.3'
        dataLicense = 'CC0-1.0'
        SPDXID = 'SPDXRef-DOCUMENT'
        name = 'DeckPipe-0.6.0+20260827.050713.6456dba254a6'
        documentNamespace = 'https://deckpipe.local/spdx/0.6.0+20260827.050713.6456dba254a6'
        creationInfo = [ordered]@{ created = '2026-08-27T05:07:13Z'; creators = @('Tool: synthetic-release-test') }
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
    Write-Utf8NoBom (Join-Path $temp 'sbom.spdx.json') ($sbom | ConvertTo-Json -Depth 10)

    $manifestLines = @()
    foreach ($relative in (@($files) + @('sbom.spdx.json')) | Sort-Object) {
        $manifestLines += "$(Get-TestSha256 (Join-Path $temp $relative))  $relative"
    }
    Write-Utf8NoBom (Join-Path $temp 'SHA256SUMS.txt') (($manifestLines -join "`n") + "`n")
    return $temp
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
    Assert-True ($version.build_id -match '^0\.6\.0\+[0-9]{8}\.[0-9]{6}\.[0-9a-f]{7,40}$') 'Build ID must include version, UTC timestamp, and opaque immutable build nonce'
    Assert-False ([bool]($version.PSObject.Properties.Name -contains 'source_revision')) 'Canonical version file must not claim source revision provenance'
    Assert-Equal $version.artifact_name_prefix "DeckPipe-$($version.build_id)" 'Artifact prefix must be stable and independent of git source revision'

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
    foreach ($relative in @('app\main.py', 'app\bugreport.py')) {
        $text = Read-TextFile $relative
        Assert-False ($text -match 'release["'']?\s*/\s*["'']?version\.json|version_path|_load_release_info|json\.load') "$relative must not read repo-layout release/version.json at import"
        Assert-True ($text -match 'APP_VERSION\s*=\s*"0\.6\.0"') "$relative must embed synchronized version"
        Assert-True ($text -match 'APP_BUILD_ID\s*=\s*"0\.6\.0\+20260827\.050713\.6456dba254a6"') "$relative must embed synchronized build id"
    }
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
        Assert-False ($text -match '(?i)SigningCertificateSubject|SubjectName|/n\s') "$relative must not sign by subject wildcard or subject name"
    }
}

It 'fails closed on stale or unsafe staging before writing release outputs' {
    $build = Join-Path $repoRoot 'release\build.ps1'
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-release-test-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        Write-Utf8NoBom (Join-Path $temp 'stale.txt') 'old output'
        Assert-Throws { & $build -StagingDirectory $temp -UnsignedEngineeringCandidate } 'nonempty|unexpected|stale'
        Assert-Equal @((Get-ChildItem -LiteralPath $temp -Force)).Count 1 'Rejected staging directory must not be cleaned or mutated'
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

It 'blocks before staging when Python hash locks are unavailable' {
    $build = Join-Path $repoRoot 'release\build.ps1'
    $verify = Join-Path $repoRoot 'release\verify.ps1'
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-release-build-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        Assert-Throws { & $build -StagingDirectory $temp -UnsignedEngineeringCandidate } 'Python lock BLOCKED'
        $verifyResult = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $temp)
        Assert-Equal $verifyResult.ExitCode 0 'BLOCKED verifier exit code'
        Assert-True ($verifyResult.Output -match 'BLOCKED')
        Assert-False (Test-Path -LiteralPath (Join-Path $temp 'release-evidence.json')) 'Blocked build must not publish evidence'
        Assert-Equal @((Get-ChildItem -LiteralPath $temp -Force)).Count 0 'Blocked build must not stage any files'
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

It 'constructs only explicit thumbprint RFC3161 SHA256 signing commands' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $args = New-SignToolArguments -CertificateThumbprint '001122AABBcc' -TimestampUrl 'https://timestamp.example/rfc3161' -ArtifactPath 'C:\out\DeckPipe.exe'
    Assert-Equal ($args -join '|') 'sign|/fd|SHA256|/sha1|001122AABBcc|/tr|https://timestamp.example/rfc3161|/td|SHA256|C:\out\DeckPipe.exe'
    Assert-False (($args -join ' ') -match '(^|\s)/a(\s|$)') 'Signing command must not auto-select a certificate'
    Assert-Throws { New-SignToolArguments -CertificateThumbprint '001122' -TimestampUrl 'http://timestamp.example' -ArtifactPath 'C:\out\DeckPipe.exe' } 'RFC3161|https'
    Assert-Throws { New-SignToolArguments -CertificateThumbprint '' -TimestampUrl 'https://timestamp.example/rfc3161' -ArtifactPath 'C:\out\DeckPipe.exe' } 'thumbprint'
    Assert-Throws { Invoke-ArtifactSigning -SignToolPath 'signtool.exe' -Artifacts @('C:\out\DeckPipe.exe') -CertificateThumbprint '001122AABBcc' -TimestampUrl 'https://timestamp.example/rfc3161' -SignExecutor { param($Tool, $Arguments) 1 } } 'signtool failed'
}

It 'rejects manifest extras missing duplicate traversal and case-confusable paths' {
    $verify = Join-Path $repoRoot 'release\verify.ps1'
    $cases = @(
        @{ Name = 'extra'; Mutate = { param($stage) Write-Utf8NoBom (Join-Path $stage 'extra.txt') 'extra' }; Pattern = 'extra|manifest' },
        @{ Name = 'missing'; Mutate = { param($stage) Add-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.txt') -Value ('0' * 64 + '  missing.exe') }; Pattern = 'missing' },
        @{ Name = 'duplicate'; Mutate = { param($stage) $hash = Get-TestSha256 (Join-Path $stage 'release-evidence.json'); Add-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.txt') -Value "$hash  release-evidence.json" }; Pattern = 'duplicate' },
        @{ Name = 'traversal'; Mutate = { param($stage) Add-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.txt') -Value ('0' * 64 + '  ../evil.exe') }; Pattern = 'traversal|escape' },
        @{ Name = 'case'; Mutate = { param($stage) $hash = Get-TestSha256 (Join-Path $stage 'release-evidence.json'); Add-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.txt') -Value "$hash  RELEASE-EVIDENCE.JSON" }; Pattern = 'duplicate|case' }
    )
    foreach ($case in $cases) {
        $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName "deckpipe-manifest-$($case.Name)"
        try {
            & $case.Mutate $stage
            $result = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $stage)
            Assert-Equal $result.ExitCode 1 "Expected FAIL exit code for $($case.Name)"
            Assert-True ($result.Output -match 'FAIL') "Expected FAIL status for $($case.Name)"
            Assert-True ($result.Output -match $case.Pattern) "Expected $($case.Pattern) for $($case.Name), got $($result.Output)"
        } finally {
            [IO.Directory]::Delete($stage, $true)
        }
    }
}

It 'blocks unsigned candidates and fails malformed release evidence' {
    $unsigned = New-SyntheticReleaseStage -StageName 'deckpipe-unsigned'
    try {
        $result = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $unsigned)
        Assert-Equal $result.ExitCode 0 "Unsigned candidate should be BLOCKED, not FAIL. Output: $($result.Output)"
        Assert-True ($result.Output -match 'BLOCKED')
        Assert-True ($result.Output -match 'signed executable|installer')
    } finally {
        [IO.Directory]::Delete($unsigned, $true)
    }

    $malformed = New-SyntheticReleaseStage -WithExecutable -StageName 'deckpipe-malformed-evidence'
    try {
        Write-Utf8NoBom (Join-Path $malformed 'release-evidence.json') '{"schema_version":1,"version":"0.6.0"}'
        $hash = Get-TestSha256 (Join-Path $malformed 'release-evidence.json')
        $manifest = Get-Content -LiteralPath (Join-Path $malformed 'SHA256SUMS.txt')
        $manifest = $manifest -replace '^[0-9a-f]{64}  release-evidence\.json$', "$hash  release-evidence.json"
        Write-Utf8NoBom (Join-Path $malformed 'SHA256SUMS.txt') (($manifest -join "`n") + "`n")
        $result = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $malformed)
        Assert-Equal $result.ExitCode 1 'Malformed evidence should be FAIL'
        Assert-True ($result.Output -match 'FAIL')
        Assert-True ($result.Output -match 'evidence')
    } finally {
        [IO.Directory]::Delete($malformed, $true)
    }
}

It 'can reach PASS only through strict manifest SBOM evidence and injected signature probe' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-pass'
    try {
        $result = Invoke-ReleaseVerification -StagingDirectory $stage -SignatureProbe {
            param($ArtifactPath)
            [pscustomobject]@{
                Status = 'Valid'
                TimeStamperCertificate = [pscustomobject]@{ Subject = 'CN=RFC3161 Test TSA'; Thumbprint = 'ABC123' }
            }
        }
        Assert-Equal $result.status 'PASS' "PASS fixture failed: $($result.message)"
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

It 'generates deterministic SPDX 2.3 with unique ids checksums and package contains relationships' {
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-sbom-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory((Join-Path $stage 'nested')) | Out-Null
    try {
        Write-Utf8NoBom (Join-Path $stage 'a_b.txt') 'alpha'
        Write-Utf8NoBom (Join-Path $stage 'nested\a-b.txt') 'beta'
        $out1 = Join-Path $stage 'sbom1.spdx.json'
        $out2 = Join-Path $stage 'sbom2.spdx.json'
        & (Join-Path $repoRoot 'release\New-SpdxSbom.ps1') -InputDirectory $stage -OutputPath $out1 -VersionJsonPath (Join-Path $repoRoot 'release\version.json')
        & (Join-Path $repoRoot 'release\New-SpdxSbom.ps1') -InputDirectory $stage -OutputPath $out2 -VersionJsonPath (Join-Path $repoRoot 'release\version.json')
        $sbom = Get-Content -LiteralPath $out1 -Raw | ConvertFrom-Json
        Assert-Equal $sbom.spdxVersion 'SPDX-2.3'
        $fileIds = @($sbom.files | ForEach-Object { $_.SPDXID })
        Assert-Equal @($fileIds | Select-Object -Unique).Count $fileIds.Count 'SPDX file IDs must be unique'
        foreach ($relative in @('a_b.txt', 'nested/a-b.txt')) {
            $file = @($sbom.files | Where-Object { $_.fileName -eq $relative })[0]
            Assert-True ($null -ne $file) "Missing SBOM file $relative"
            $expectedHash = Get-TestSha256 (Join-Path $stage ($relative -replace '/', '\'))
            Assert-Equal $file.checksums[0].checksumValue $expectedHash "Checksum mismatch for $relative"
            $contains = @($sbom.relationships | Where-Object { $_.spdxElementId -eq 'SPDXRef-Package-DeckPipe' -and $_.relationshipType -eq 'CONTAINS' -and $_.relatedSpdxElement -eq $file.SPDXID })
            Assert-Equal $contains.Count 1 "Missing package CONTAINS relationship for $relative"
        }
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
