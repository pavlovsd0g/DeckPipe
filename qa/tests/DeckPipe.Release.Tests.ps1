$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$script:ExpectedPrivateBetaPolicy = [ordered]@{
    schema_version = 1
    channel = 'private-beta'
    signing_requirement = 'owner-waived'
    timestamp_requirement = 'owner-waived'
    windows_reputation_warning = 'accepted'
    waiver_date = '2026-08-28'
    intended_audience = 'controlled-small-group'
}

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

function New-TestLabCaseRoot {
    param([string]$Name)
    $root = Join-Path 'D:\DeckPipe-RC-Lab\qa-evidence\release-tests' ("$Name-" + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($root) | Out-Null
    return $root
}

function New-TestWheelhouse {
    param(
        [string]$Root,
        [string[]]$WheelNames = @(
            'alpha-1.0.0-py3-none-any.whl',
            'bravo-2.0.0-cp312-cp312-win_amd64.whl'
        )
    )
    $wheelhouse = Join-Path $Root 'wheelhouse'
    [IO.Directory]::CreateDirectory($wheelhouse) | Out-Null
    $records = @()
    foreach ($name in @($WheelNames | Sort-Object)) {
        $path = Join-Path $wheelhouse $name
        Write-Utf8NoBom $path "synthetic wheel bytes for $name"
        $item = Get-Item -LiteralPath $path
        $records += [ordered]@{
            name = $name
            size = [int64]$item.Length
            sha256 = Get-TestSha256 $path
        }
    }
    $manifest = [ordered]@{
        schema_version = 1
        files = $records
    }
    Write-Utf8NoBom (Join-Path $wheelhouse 'wheelhouse-manifest.json') ($manifest | ConvertTo-Json -Depth 6)
    return $wheelhouse
}

function Get-ReleaseTestPythonExe {
    $candidate = Join-Path $repoRoot '..\..\.venv\Scripts\python.exe'
    return (Resolve-Path -LiteralPath $candidate).Path
}

function Get-TestCurrentHead {
    $revisionOutput = & git -C $repoRoot rev-parse HEAD 2>$null
    $revisionExitCode = $LASTEXITCODE
    $revisionItems = @($revisionOutput | Select-Object -First 1)
    $revision = if ($revisionItems.Count -gt 0) { $revisionItems[0] } else { '' }
    if (-not $revision -or $revisionExitCode -ne 0) { throw 'test setup failed: git rev-parse HEAD failed' }
    return [string]$revision
}

function New-TestPrivateBetaPolicy {
    return [ordered]@{
        schema_version = 1
        channel = 'private-beta'
        signing_requirement = 'owner-waived'
        timestamp_requirement = 'owner-waived'
        windows_reputation_warning = 'accepted'
        waiver_date = '2026-08-28'
        intended_audience = 'controlled-small-group'
    }
}

function New-SyntheticReleaseStage {
    param(
        [switch]$WithExecutable,
        [switch]$SignedEvidence,
        [string]$StageName = 'deckpipe-release-stage',
        [string]$BuildId = '0.6.0+20260827.050713.6456dba254a6',
        [string]$SourceRevision = ''
    )
    if (-not $SourceRevision) { $SourceRevision = Get-TestCurrentHead }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ($StageName + '-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $files = @()
    $sourceShort = $SourceRevision.Substring(0, 7)
    $evidence = [ordered]@{
        schema_version = 1
        product = 'DeckPipe'
        version = '0.6.0'
        build_id = $BuildId
        source_revision = $SourceRevision
        artifacts = @()
        signing = [ordered]@{ status = if ($SignedEvidence) { 'PASS' } else { 'BLOCKED' }; signed = @() }
        timestamp = [ordered]@{ status = if ($SignedEvidence) { 'PASS' } else { 'BLOCKED' } }
    }
    if ($WithExecutable) {
        $artifactName = "DeckPipe-$BuildId-$sourceShort-x64.exe"
        $artifactPath = Join-Path $temp $artifactName
        Write-Utf8NoBom $artifactPath 'synthetic installer bytes'
        $hash = Get-TestSha256 $artifactPath
        $evidence.artifacts += [ordered]@{ path = $artifactName; type = 'exe'; sha256 = $hash }
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
        name = "DeckPipe-$BuildId"
        documentNamespace = "https://deckpipe.local/spdx/$BuildId"
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

function Set-SyntheticPrivateBetaPolicy {
    param(
        [Parameter(Mandatory)][string]$StagePath,
        $Policy = $null,
        [string]$PolicyPath = 'policy.json',
        [string]$PolicySha256 = '',
        [string]$Channel = 'private-beta',
        [string]$ArtifactLabel = 'unsigned-private-beta',
        [string]$SigningStatus = 'WAIVED_BY_OWNER',
        [string[]]$Signed = @(),
        [string]$TimestampStatus = 'WAIVED_BY_OWNER'
    )

    $policyRelative = if ($PolicyPath -eq 'policy.json') { 'policy.json' } else { 'policy.json' }
    if ($null -eq $Policy) {
        Copy-Item -LiteralPath (Join-Path $repoRoot 'release\policy.json') -Destination (Join-Path $StagePath $policyRelative) -Force
    } else {
        Write-Utf8NoBom (Join-Path $StagePath $policyRelative) ($Policy | ConvertTo-Json -Depth 8)
    }
    $policyHash = if ($PolicySha256) { $PolicySha256 } else { Get-TestSha256 (Join-Path $StagePath $policyRelative) }
    $evidencePath = Join-Path $StagePath 'release-evidence.json'
    $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
    foreach ($artifact in @($evidence.artifacts)) {
        $oldName = [string]$artifact.path
        if ($oldName -notmatch '-unsigned-private-beta-x64') {
            $newName = $oldName -replace '-x64', '-unsigned-private-beta-x64'
            Rename-Item -LiteralPath (Join-Path $StagePath $oldName) -NewName $newName
            $artifact.path = $newName
            $artifact.sha256 = Get-TestSha256 (Join-Path $StagePath $newName)
        }
    }
    $normalizedSigned = @()
    foreach ($signedPath in @($Signed)) {
        $normalizedSigned += ([string]$signedPath -replace '-x64', '-unsigned-private-beta-x64')
    }
    $evidence | Add-Member -Force -NotePropertyName distribution -NotePropertyValue ([pscustomobject][ordered]@{
        channel = $Channel
        artifact_label = $ArtifactLabel
        policy_path = $PolicyPath
        policy_sha256 = $policyHash
    })
    $evidence.signing.status = $SigningStatus
    $evidence.signing.signed = $normalizedSigned
    $evidence.signing | Add-Member -Force -NotePropertyName policy_path -NotePropertyValue $PolicyPath
    $evidence.timestamp.status = $TimestampStatus
    $evidence.timestamp | Add-Member -Force -NotePropertyName policy_path -NotePropertyValue $PolicyPath
    Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 10)
    Update-SyntheticStageInventory $StagePath
}

function Convert-SyntheticPolicyToUtf16WithBoundHash {
    param([Parameter(Mandatory)][string]$StagePath)
    $policyPath = Join-Path $StagePath 'policy.json'
    $trackedRaw = Get-Content -LiteralPath (Join-Path $repoRoot 'release\policy.json') -Raw
    [IO.File]::WriteAllText($policyPath, $trackedRaw, [Text.UnicodeEncoding]::new($false, $true))
    $evidencePath = Join-Path $StagePath 'release-evidence.json'
    $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
    $evidence.distribution.policy_sha256 = Get-TestSha256 $policyPath
    Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 10)
    Update-SyntheticStageInventory $StagePath
}

function Update-SyntheticStageInventory {
    param([string]$StagePath)
    $sbomFiles = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $StagePath -Recurse -Force -File | Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'sbom.spdx.json') } | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($StagePath.Length).TrimStart('\') -replace '\\', '/'
        $spdxId = 'SPDXRef-File-' + (Get-TestSha256 $file.FullName).Substring(0, 16)
        $sbomFiles += [ordered]@{
            SPDXID = $spdxId
            fileName = $relative
            checksums = @([ordered]@{ algorithm = 'SHA256'; checksumValue = Get-TestSha256 $file.FullName })
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
    Write-Utf8NoBom (Join-Path $StagePath 'sbom.spdx.json') ($sbom | ConvertTo-Json -Depth 10)

    $manifestLines = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $StagePath -Recurse -Force -File | Where-Object { $_.Name -ne 'SHA256SUMS.txt' } | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($StagePath.Length).TrimStart('\') -replace '\\', '/'
        $manifestLines += "$(Get-TestSha256 $file.FullName)  $relative"
    }
    Write-Utf8NoBom (Join-Path $StagePath 'SHA256SUMS.txt') (($manifestLines -join "`n") + "`n")
}

function Invoke-SyntheticPassProbe {
    param([string]$StagePath)
    . (Join-Path $repoRoot 'release\verify.ps1')
    return Invoke-ReleaseVerification -StagingDirectory $StagePath -SignatureProbe {
        param($ArtifactPath)
        [pscustomobject]@{
            Status = 'Valid'
            TimeStamperCertificate = [pscustomobject]@{ Subject = 'CN=RFC3161 Test TSA'; Thumbprint = 'ABC123' }
        }
    } -ExpectedSourceRevision (Get-TestCurrentHead)
}

function New-TestGitMetadataFixture {
    param(
        [string]$Name,
        [string]$Revision,
        [switch]$Packed,
        [switch]$LinkedWorktree,
        [string]$RefName = 'refs/heads/main',
        [string]$HeadContent = '',
        [string]$PackedContent = ''
    )
    $root = Join-Path ([IO.Path]::GetTempPath()) ($Name + '-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($root) | Out-Null
    if ($LinkedWorktree) {
        $common = Join-Path $root 'common.git'
        $gitDir = Join-Path $common 'worktrees\fixture'
        [IO.Directory]::CreateDirectory($gitDir) | Out-Null
        Write-Utf8NoBom (Join-Path $root '.git') "gitdir: $gitDir`n"
        Write-Utf8NoBom (Join-Path $gitDir 'commondir') "..\..`n"
        if ($HeadContent) {
            Write-Utf8NoBom (Join-Path $gitDir 'HEAD') $HeadContent
        } else {
            Write-Utf8NoBom (Join-Path $gitDir 'HEAD') "ref: $RefName`n"
        }
        $refRoot = $common
    } else {
        $gitDir = Join-Path $root '.git'
        [IO.Directory]::CreateDirectory($gitDir) | Out-Null
        if ($HeadContent) {
            Write-Utf8NoBom (Join-Path $gitDir 'HEAD') $HeadContent
        } else {
            Write-Utf8NoBom (Join-Path $gitDir 'HEAD') "ref: $RefName`n"
        }
        $refRoot = $gitDir
    }
    if ($Packed) {
        if ($PackedContent) {
            Write-Utf8NoBom (Join-Path $refRoot 'packed-refs') $PackedContent
        } else {
            Write-Utf8NoBom (Join-Path $refRoot 'packed-refs') "# pack-refs with: peeled fully-peeled sorted`n$Revision $RefName`n"
        }
    } else {
        $refPath = Join-Path $refRoot ($RefName -replace '/', '\')
        Write-Utf8NoBom $refPath "$Revision`n"
    }
    return $root
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

It 'defines exactly one tracked private beta waiver policy' {
    $policyPath = Join-Path $repoRoot 'release\policy.json'
    Assert-True (Test-Path -LiteralPath $policyPath -PathType Leaf) 'Missing tracked release/policy.json'
    $policy = Get-Content -LiteralPath $policyPath -Raw | ConvertFrom-Json
    $actualNames = @($policy.PSObject.Properties.Name)
    $expectedNames = @($script:ExpectedPrivateBetaPolicy.Keys)
    Assert-Equal ($actualNames -join '|') ($expectedNames -join '|') 'Private beta policy properties must be exact and in canonical order'
    foreach ($name in $expectedNames) {
        Assert-Equal $policy.$name $script:ExpectedPrivateBetaPolicy[$name] "Private beta policy drift: $name"
    }
}

It 'rejects private beta policy schema_version type drift in verifier policy validation' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $policy = [pscustomobject][ordered]@{
        schema_version = '1'
        channel = 'private-beta'
        signing_requirement = 'owner-waived'
        timestamp_requirement = 'owner-waived'
        windows_reputation_warning = 'accepted'
        waiver_date = '2026-08-28'
        intended_audience = 'controlled-small-group'
    }
    Assert-Throws { Assert-ExactPrivateBetaPolicyObject -Policy $policy -Context 'test' } 'schema_version|integer|type'
}

It 'records hash-enforced Python runtime and build locks without blocked or unsafe requirements' {
    foreach ($relative in @('pyproject.toml', 'requirements.in', 'requirements-build.in', 'requirements.lock', 'requirements-build.lock')) {
        Assert-True (Test-Path -LiteralPath (Join-Path $repoRoot $relative)) "Missing $relative"
    }
    foreach ($relative in @('requirements.lock', 'requirements-build.lock')) {
        $text = Read-TextFile $relative
        Assert-False ($text -match '(?m)^# LOCK-STATUS: BLOCKED$') "$relative must not carry a blocked release status"
        Assert-True ($text -match '(?m)^--require-hashes$') "$relative must force pip hash checking"
        Assert-True ($text -match '--hash=sha256:[0-9a-f]{64}') "$relative must contain real lowercase SHA-256 hashes"
        Assert-False ($text -match '(?im)^\s*(-e|--editable|https?://|git\+)') "$relative must not contain editable URL or VCS requirements"
        Assert-False ($text -match '(?i)PLACEHOLDER_HASH|FAKE_HASH|examplehash') "$relative contains placeholder hashes"
        $requirementLines = @($text -split "`r?`n" | Where-Object { $_ -match '^[A-Za-z0-9_.-]+==' })
        Assert-True ($requirementLines.Count -gt 0) "$relative must contain pinned requirements"
        foreach ($line in $requirementLines) {
            Assert-True ($line -match '^[A-Za-z0-9_.-]+==[^\\\s]+') "$relative contains an unpinned requirement line: $line"
        }
    }
}

It 'ships PowerShell 5.1 parseable release scripts without certificate-store discovery' {
    foreach ($relative in @('release\build.ps1', 'release\prepare-wheelhouse.ps1', 'release\verify.ps1', 'release\New-SpdxSbom.ps1')) {
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
    . (Join-Path $repoRoot 'release\build.ps1')
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-release-test-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        Write-Utf8NoBom (Join-Path $temp 'stale.txt') 'old output'
        Assert-Throws { Assert-StagingDirectorySafe $temp } 'staging directory is nonempty'
        Assert-Equal @((Get-ChildItem -LiteralPath $temp -Force)).Count 1 'Rejected staging directory must not be cleaned or mutated'
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

It 'blocks before staging when no explicit offline wheelhouse is supplied' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-release-build-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    try {
        $version = Read-JsonFile 'release\version.json'
        Assert-Throws {
            New-ReleaseBuildPlan -StagingDirectory $temp -Version $version -SourceRevision 'dddddddddddddddddddddddddddddddddddddddd' -PythonExe 'python.exe'
        } 'wheelhouse'
        $verifyResult = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $temp)
        Assert-Equal $verifyResult.ExitCode 2 'BLOCKED verifier exit code'
        $blockedJson = $verifyResult.Output | ConvertFrom-Json
        Assert-Equal $blockedJson.status 'BLOCKED'
        Assert-False (Test-Path -LiteralPath (Join-Path $temp 'release-evidence.json')) 'Blocked build must not publish evidence'
        Assert-Equal @((Get-ChildItem -LiteralPath $temp -Force)).Count 0 'Blocked build must not stage any files'
    } finally {
        [IO.Directory]::Delete($temp, $true)
    }
}

It 'reports a missing staging parent before creating release directories' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $missingRoot = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-missing-parent-' + [guid]::NewGuid().ToString('N'))
    $stage = Join-Path $missingRoot 'stage'
    try {
        Assert-Throws { Assert-StagingDirectorySafe $stage } 'staging directory parent is missing'
        Assert-False (Test-Path -LiteralPath $missingRoot) 'Blocked build must not create the missing staging parent'
    } finally {
        if (Test-Path -LiteralPath $missingRoot) { [IO.Directory]::Delete($missingRoot, $true) }
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

It 'rejects signed-looking evidence whose build id or source revision is not canonical current HEAD' {
    $alternateBuildId = '0.6.0+20260827.050713.deadbee'
    $alternateSource = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
    $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-stale-freshness' -BuildId $alternateBuildId -SourceRevision $alternateSource
    try {
        . (Join-Path $repoRoot 'release\verify.ps1')
        $result = Invoke-SyntheticPassProbe $stage
        Assert-Equal $result.status 'FAIL' "Verifier must reject stale signed-looking evidence, got $($result.status): $($result.message)"
        Assert-True ($result.message -match 'canonical|build_id|source|HEAD|fresh') "Expected freshness failure, got $($result.message)"
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

It 'derives source revision from git metadata without trusting caller PATH git' {
    $spoofedSource = '0123456789abcdef0123456789abcdef01234567'
    $fakeBin = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-fake-git-' + [guid]::NewGuid().ToString('N'))
    $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-hostile-path' -SourceRevision $spoofedSource
    $oldPath = $env:PATH
    try {
        [IO.Directory]::CreateDirectory($fakeBin) | Out-Null
        Write-Utf8NoBom (Join-Path $fakeBin 'git.cmd') "@echo off`r`necho $spoofedSource`r`nexit /b 0`r`n"
        $env:PATH = $fakeBin + [IO.Path]::PathSeparator + $oldPath
        . (Join-Path $repoRoot 'release\verify.ps1')

        $result = Invoke-ReleaseVerification -StagingDirectory $stage -SignatureProbe {
            param($ArtifactPath)
            [pscustomobject]@{
                Status = 'Valid'
                TimeStamperCertificate = [pscustomobject]@{ Subject = 'CN=RFC3161 Test TSA'; Thumbprint = 'ABC123' }
            }
        }

        Assert-Equal $result.status 'FAIL' "Verifier must reject PATH-spoofed source revision, got $($result.status): $($result.message)"
        Assert-True ($result.message -match 'source_revision|current HEAD|source provenance') "Expected provenance failure, got $($result.message)"
    } finally {
        $env:PATH = $oldPath
        if (Test-Path -LiteralPath $stage) { [IO.Directory]::Delete($stage, $true) }
        if (Test-Path -LiteralPath $fakeBin) { [IO.Directory]::Delete($fakeBin, $true) }
    }
}

It 'reads current source revision from loose packed and linked worktree git metadata' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $cases = @(
        @{ Name = 'loose'; Revision = '1111111111111111111111111111111111111111'; Packed = $false; Linked = $false },
        @{ Name = 'packed'; Revision = '2222222222222222222222222222222222222222'; Packed = $true; Linked = $false },
        @{ Name = 'linked-packed'; Revision = '3333333333333333333333333333333333333333'; Packed = $true; Linked = $true },
        @{ Name = 'detached'; Revision = '4444444444444444444444444444444444444444'; Packed = $false; Linked = $false; HeadContent = "4444444444444444444444444444444444444444`n" }
    )
    $originalRepoRoot = $script:RepoRoot
    foreach ($case in $cases) {
        $headContent = if ($case.ContainsKey('HeadContent')) { [string]$case.HeadContent } else { '' }
        $fixture = New-TestGitMetadataFixture -Name "deckpipe-git-$($case.Name)" -Revision $case.Revision -Packed:([bool]$case.Packed) -LinkedWorktree:([bool]$case.Linked) -HeadContent $headContent
        try {
            $script:RepoRoot = $fixture
            Assert-Equal (Get-CurrentSourceRevision) $case.Revision "metadata revision mismatch for $($case.Name)"
        } finally {
            $script:RepoRoot = $originalRepoRoot
            [IO.Directory]::Delete($fixture, $true)
        }
    }
}

It 'blocks uppercase git revisions in detached loose and packed metadata' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $uppercase = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    $cases = @(
        @{ Name = 'detached-uppercase'; Packed = $false; HeadContent = "$uppercase`n" },
        @{ Name = 'loose-uppercase'; Packed = $false; HeadContent = '' },
        @{ Name = 'packed-uppercase'; Packed = $true; HeadContent = '' }
    )
    $originalRepoRoot = $script:RepoRoot
    foreach ($case in $cases) {
        $fixture = New-TestGitMetadataFixture -Name "deckpipe-git-$($case.Name)" -Revision $uppercase -Packed:([bool]$case.Packed) -HeadContent ([string]$case.HeadContent)
        try {
            $script:RepoRoot = $fixture
            Assert-Throws { Get-CurrentSourceRevision | Out-Null } 'source provenance BLOCKED|lowercase'
        } finally {
            $script:RepoRoot = $originalRepoRoot
            [IO.Directory]::Delete($fixture, $true)
        }
    }
}

It 'rejects whitespace-padded git object ids without normalization' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $revision = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    $cases = @(
        @{ Name = 'detached-leading-space'; Packed = $false; HeadContent = " $revision`n" },
        @{ Name = 'detached-trailing-space'; Packed = $false; HeadContent = "$revision `n" },
        @{ Name = 'loose-leading-space'; Packed = $false; Revision = " $revision" },
        @{ Name = 'loose-trailing-space'; Packed = $false; Revision = "$revision " },
        @{ Name = 'packed-leading-space'; Packed = $true; PackedContent = "# pack-refs with: peeled fully-peeled sorted`n $revision refs/heads/main`n" },
        @{ Name = 'packed-trailing-space'; Packed = $true; PackedContent = "# pack-refs with: peeled fully-peeled sorted`n$revision refs/heads/main `n" }
    )
    $originalRepoRoot = $script:RepoRoot
    foreach ($case in $cases) {
        $fixtureRevision = if ($case.ContainsKey('Revision')) { [string]$case.Revision } else { $revision }
        $headContent = if ($case.ContainsKey('HeadContent')) { [string]$case.HeadContent } else { '' }
        $packedContent = if ($case.ContainsKey('PackedContent')) { [string]$case.PackedContent } else { '' }
        $fixture = New-TestGitMetadataFixture -Name "deckpipe-git-$($case.Name)" -Revision $fixtureRevision -Packed:([bool]$case.Packed) -HeadContent $headContent -PackedContent $packedContent
        try {
            $script:RepoRoot = $fixture
            Assert-Throws { Get-CurrentSourceRevision | Out-Null } 'source provenance BLOCKED|lowercase|malformed'
        } finally {
            $script:RepoRoot = $originalRepoRoot
            [IO.Directory]::Delete($fixture, $true)
        }
    }
}

It 'matches git refs case-sensitively across loose and packed metadata' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $revision = '5555555555555555555555555555555555555555'
    $cases = @(
        @{ Name = 'loose-case-confusable'; Packed = $false },
        @{ Name = 'packed-case-confusable'; Packed = $true }
    )
    $originalRepoRoot = $script:RepoRoot
    foreach ($case in $cases) {
        $fixture = New-TestGitMetadataFixture -Name "deckpipe-git-$($case.Name)" -Revision $revision -Packed:([bool]$case.Packed) -RefName 'refs/heads/main' -HeadContent "ref: refs/heads/Main`n"
        try {
            $script:RepoRoot = $fixture
            Assert-Throws { Get-CurrentSourceRevision | Out-Null } 'source provenance BLOCKED|unavailable|case'
        } finally {
            $script:RepoRoot = $originalRepoRoot
            [IO.Directory]::Delete($fixture, $true)
        }
    }
}

It 'blocks multiple loose git ref candidates across linked git directories' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $revisionOne = '8888888888888888888888888888888888888888'
    $revisionTwo = '9999999999999999999999999999999999999999'
    $cases = @(
        @{ Name = 'same-revision'; WorktreeRevision = $revisionOne; CommonRevision = $revisionOne },
        @{ Name = 'different-revision'; WorktreeRevision = $revisionTwo; CommonRevision = $revisionOne }
    )
    $originalRepoRoot = $script:RepoRoot
    foreach ($case in $cases) {
        $fixture = New-TestGitMetadataFixture -Name "deckpipe-git-linked-loose-$($case.Name)" -Revision $case.CommonRevision -LinkedWorktree
        try {
            $script:RepoRoot = $fixture
            $worktreeGitDir = Join-Path $fixture 'common.git\worktrees\fixture'
            Write-Utf8NoBom (Join-Path $worktreeGitDir 'refs\heads\main') "$($case.WorktreeRevision)`n"
            Assert-Throws { Get-CurrentSourceRevision | Out-Null } 'source provenance BLOCKED|ambiguous|duplicate|loose'
        } finally {
            $script:RepoRoot = $originalRepoRoot
            [IO.Directory]::Delete($fixture, $true)
        }
    }
}

It 'uses one loose git ref ahead of packed refs with git precedence' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $looseRevision = 'abcdefabcdefabcdefabcdefabcdefabcdefabcd'
    $packedRevision = '1234567890abcdef1234567890abcdef12345678'
    $fixture = New-TestGitMetadataFixture -Name 'deckpipe-git-loose-over-packed' -Revision $packedRevision -Packed
    $originalRepoRoot = $script:RepoRoot
    try {
        Write-Utf8NoBom (Join-Path $fixture '.git\refs\heads\main') "$looseRevision`n"
        $script:RepoRoot = $fixture
        Assert-Equal (Get-CurrentSourceRevision) $looseRevision
    } finally {
        $script:RepoRoot = $originalRepoRoot
        [IO.Directory]::Delete($fixture, $true)
    }
}

It 'blocks duplicate ambiguous and malformed packed refs' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $revisionOne = '6666666666666666666666666666666666666666'
    $revisionTwo = '7777777777777777777777777777777777777777'
    $validPacked = "# pack-refs with: peeled fully-peeled sorted`n$revisionOne refs/heads/main`n"
    $cases = @(
        @{
            Name = 'duplicate-target'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`n$revisionOne refs/heads/main`n$revisionTwo refs/heads/main`n"
        },
        @{
            Name = 'malformed-before-target'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`nnot-a-revision refs/heads/other`n$revisionOne refs/heads/main`n"
        },
        @{
            Name = 'malformed-peeled-line'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`n$revisionOne refs/tags/v1`n^not-a-revision`n$revisionTwo refs/heads/main`n"
        },
        @{
            Name = 'case-confusable-target'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`n$revisionOne refs/heads/Main`n"
        },
        @{
            Name = 'orphan-peeled-line'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`n^$revisionTwo`n$revisionOne refs/heads/main`n"
        },
        @{
            Name = 'repeated-peeled-line'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`n$revisionTwo refs/tags/v1`n^$revisionOne`n^$revisionTwo`n$revisionOne refs/heads/main`n"
        },
        @{
            Name = 'duplicate-nontarget-record'
            PackedContent = "# pack-refs with: peeled fully-peeled sorted`n$revisionTwo refs/tags/v1`n$revisionOne refs/tags/v1`n$revisionOne refs/heads/main`n"
        }
    )
    $originalRepoRoot = $script:RepoRoot
    foreach ($case in $cases) {
        $fixture = New-TestGitMetadataFixture -Name "deckpipe-git-$($case.Name)" -Revision $revisionOne -Packed -PackedContent $case.PackedContent
        try {
            $script:RepoRoot = $fixture
            Assert-Throws { Get-CurrentSourceRevision | Out-Null } 'source provenance BLOCKED|duplicate|ambiguous|malformed|unavailable'
        } finally {
            $script:RepoRoot = $originalRepoRoot
            [IO.Directory]::Delete($fixture, $true)
        }
    }

    $linkedFixture = New-TestGitMetadataFixture -Name 'deckpipe-git-linked-ambiguous-packed' -Revision $revisionOne -Packed -LinkedWorktree -PackedContent $validPacked
    try {
        $script:RepoRoot = $linkedFixture
        $worktreeGitDir = Join-Path $linkedFixture 'common.git\worktrees\fixture'
        Write-Utf8NoBom (Join-Path $worktreeGitDir 'packed-refs') "# pack-refs with: peeled fully-peeled sorted`n$revisionTwo refs/heads/main`n"
        Assert-Throws { Get-CurrentSourceRevision | Out-Null } 'source provenance BLOCKED|duplicate|ambiguous'
    } finally {
        $script:RepoRoot = $originalRepoRoot
        [IO.Directory]::Delete($linkedFixture, $true)
    }
}

It 'blocks symbolic git ref names rejected by check-ref-format rules' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $gitDir = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-git-ref-format-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($gitDir) | Out-Null
    $unsafeNames = @(
        'refs/heads/main.lock',
        'refs/heads/.main',
        'refs/heads/main.',
        'refs/heads/main bad',
        ("refs/heads/main" + [char]1 + "bad"),
        'refs/heads/main~bad',
        'refs/heads/main^bad',
        'refs/heads/main:bad',
        'refs/heads/main?bad',
        'refs/heads/main*bad',
        'refs/heads/main[bad',
        'refs\heads\main',
        'refs/heads/main..bad',
        'refs/heads/main@{bad',
        'refs/heads//main',
        '/refs/heads/main',
        '../refs/heads/main',
        'refs/heads/../main',
        'refs/heads/main/',
        'refs',
        '@'
    )
    try {
        foreach ($name in $unsafeNames) {
            Assert-False (Test-SafeGitRefName $name) "Unsafe ref name accepted by predicate: $name"
            Assert-Throws { Get-GitRefRevision -GitDirectories @($gitDir) -RefName $name | Out-Null } 'source provenance BLOCKED|unsafe'
        }
    } finally {
        [IO.Directory]::Delete($gitDir, $true)
    }
}

It 'blocks verification when trusted git metadata is missing or malformed' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $fixture = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-git-malformed-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory((Join-Path $fixture '.git')) | Out-Null
    Write-Utf8NoBom (Join-Path $fixture '.git\HEAD') "ref: refs/heads/main`n"
    $originalRepoRoot = $script:RepoRoot
    $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-metadata-blocked'
    try {
        $script:RepoRoot = $fixture
        $result = Invoke-ReleaseVerification -StagingDirectory $stage -SignatureProbe {
            param($ArtifactPath)
            [pscustomobject]@{
                Status = 'Valid'
                TimeStamperCertificate = [pscustomobject]@{ Subject = 'CN=RFC3161 Test TSA'; Thumbprint = 'ABC123' }
            }
        }
        Assert-Equal $result.status 'BLOCKED' "Malformed metadata must block verifier, got $($result.status): $($result.message)"
        Assert-True ($result.message -match 'source provenance BLOCKED') "Expected source provenance BLOCKED, got $($result.message)"
    } finally {
        $script:RepoRoot = $originalRepoRoot
        if ($stage -and (Test-Path -LiteralPath $stage)) { [IO.Directory]::Delete($stage, $true) }
        if (Test-Path -LiteralPath $fixture) { [IO.Directory]::Delete($fixture, $true) }
    }
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
        Assert-Equal $result.ExitCode 1 "Empty artifact evidence should be malformed FAIL. Output: $($result.Output)"
        Assert-True ($result.Output -match 'FAIL')
        Assert-True ($result.Output -match 'artifacts must be nonempty')
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
        $failJson = $result.Output | ConvertFrom-Json
        Assert-Equal $failJson.status 'FAIL'
        Assert-True ($failJson.message -match 'evidence')
    } finally {
        [IO.Directory]::Delete($malformed, $true)
    }
}

It 'allows private beta PASS only when exact tracked policy and evidence hash are bound' {
    $stage = New-SyntheticReleaseStage -WithExecutable -StageName 'deckpipe-private-beta-pass'
    try {
        Set-SyntheticPrivateBetaPolicy -StagePath $stage
        $result = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $stage)
        Assert-Equal $result.ExitCode 0 "Private beta verifier exit code. Output: $($result.Output)"
        $json = $result.Output | ConvertFrom-Json
        Assert-Equal $json.status 'PASS'
        Assert-True ($json.message -match 'private-beta|policy|waiver') "Expected private beta policy waiver message, got $($json.message)"
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

It 'rejects private beta policy drift forged hashes caller policy paths and signed-looking waiver evidence' {
    $cases = @(
        @{
            Name = 'missing-policy-field'
            Mutate = {
                param($stage)
                $policy = New-TestPrivateBetaPolicy
                $policy.Remove('intended_audience')
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -Policy $policy
            }
            Pattern = 'policy|missing|intended_audience|exact'
        },
        @{
            Name = 'extra-policy-field'
            Mutate = {
                param($stage)
                $policy = New-TestPrivateBetaPolicy
                $policy['approved_by'] = 'owner'
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -Policy $policy
            }
            Pattern = 'policy|extra|approved_by|exact'
        },
        @{
            Name = 'public-channel'
            Mutate = {
                param($stage)
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -Channel 'public'
            }
            Pattern = 'private-beta|public|channel|policy'
        },
        @{
            Name = 'schema-version-string'
            Mutate = {
                param($stage)
                $policy = New-TestPrivateBetaPolicy
                $policy['schema_version'] = '1'
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -Policy $policy
            }
            Pattern = 'schema_version|integer|type|policy|sha256|tracked|byte'
        },
        @{
            Name = 'same-semantics-utf16-policy'
            Mutate = {
                param($stage)
                Set-SyntheticPrivateBetaPolicy -StagePath $stage
                Convert-SyntheticPolicyToUtf16WithBoundHash -StagePath $stage
            }
            Pattern = 'policy|sha256|tracked|byte'
        },
        @{
            Name = 'caller-policy-path'
            Mutate = {
                param($stage)
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -PolicyPath (Join-Path $stage 'policy.json')
            }
            Pattern = 'policy_path|policy\.json|caller|path'
        },
        @{
            Name = 'forged-policy-hash'
            Mutate = {
                param($stage)
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -PolicySha256 ('0' * 64)
            }
            Pattern = 'policy|sha256|hash|mismatch'
        },
        @{
            Name = 'pass-signing-without-valid-signature'
            Mutate = {
                param($stage)
                $artifact = @(Get-ChildItem -LiteralPath $stage -Force -File | Where-Object { $_.Name -match '\.exe$' })[0]
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -SigningStatus 'PASS' -TimestampStatus 'PASS' -Signed @($artifact.Name)
            }
            Pattern = 'Authenticode|signature|timestamp|PASS'
        },
        @{
            Name = 'waived-without-exact-private-beta-policy'
            Mutate = {
                param($stage)
                $policy = New-TestPrivateBetaPolicy
                $policy['channel'] = 'public'
                Set-SyntheticPrivateBetaPolicy -StagePath $stage -Policy $policy
            }
            Pattern = 'private-beta|policy|WAIVED_BY_OWNER|exact'
        }
    )

    foreach ($case in $cases) {
        $stage = New-SyntheticReleaseStage -WithExecutable -StageName "deckpipe-private-beta-$($case.Name)"
        try {
            & $case.Mutate $stage
            $result = Invoke-ReleaseScript 'release\verify.ps1' @('-StagingDirectory', $stage)
            Assert-False ($result.ExitCode -eq 0) "Expected verifier rejection for $($case.Name), got PASS: $($result.Output)"
            Assert-True ($result.Output -match $case.Pattern) "Expected $($case.Pattern) for $($case.Name), got $($result.Output)"
        } finally {
            [IO.Directory]::Delete($stage, $true)
        }
    }
}

It 'rejects artifact evidence sets that omit actual artifacts or signed paths' {
    $cases = @(
        @{
            Name = 'omitted-actual-artifact'
            Mutate = {
                param($stage)
                Write-Utf8NoBom (Join-Path $stage 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-09767c4-x64.msi') 'synthetic msi bytes'
                Update-SyntheticStageInventory $stage
            }
            Pattern = 'artifact|evidence|signed|omitted|mismatch|bijection'
        },
        @{
            Name = 'empty-artifacts-and-signed'
            Mutate = {
                param($stage)
                $evidencePath = Join-Path $stage 'release-evidence.json'
                $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
                $evidence.artifacts = @()
                $evidence.signing.signed = @()
                Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 10)
                Update-SyntheticStageInventory $stage
            }
            Pattern = 'artifact|signed|empty|nonempty|mismatch'
        },
        @{
            Name = 'signed-set-mismatch'
            Mutate = {
                param($stage)
                $evidencePath = Join-Path $stage 'release-evidence.json'
                $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
                $evidence.signing.signed = @('DeckPipe-0.6.0+20260827.050713.6456dba254a6-09767c4-x64.msi')
                Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 10)
                Update-SyntheticStageInventory $stage
            }
            Pattern = 'signed|artifact|mismatch|missing'
        }
    )

    $failures = @()
    foreach ($case in $cases) {
        $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName "deckpipe-evidence-$($case.Name)"
        try {
            & $case.Mutate $stage
            $result = Invoke-SyntheticPassProbe $stage
            if ($result.status -eq 'PASS') {
                $failures += "accepted $($case.Name): $($result.message)"
            } elseif ($result.message -notmatch $case.Pattern) {
                $failures += "wrong message for $($case.Name): $($result.message)"
            }
        } finally {
            [IO.Directory]::Delete($stage, $true)
        }
    }
    if ($failures.Count -gt 0) { throw ($failures -join '; ') }
}

It 'rejects fully inventoried staging extras nested artifacts and evidence duplicates' {
    $cases = @(
        @{
            Name = 'manifest-and-sbom-backed-extra'
            Mutate = {
                param($stage)
                Write-Utf8NoBom (Join-Path $stage 'extra.txt') 'extra'
                Update-SyntheticStageInventory $stage
            }
            Pattern = 'extra|allowlist|top-level|staged'
        },
        @{
            Name = 'nested-artifact'
            Mutate = {
                param($stage)
                $existingArtifact = @(Get-ChildItem -LiteralPath $stage -Force -File | Where-Object { $_.Name -match '\.exe$' })[0]
                Remove-Item -LiteralPath $existingArtifact.FullName -Force
                [IO.Directory]::CreateDirectory((Join-Path $stage 'nested')) | Out-Null
                $artifact = 'nested/' + ($existingArtifact.Name -replace '-x64\.exe$', '-x64-setup.exe')
                Write-Utf8NoBom (Join-Path $stage ($artifact -replace '/', '\')) 'nested artifact bytes'
                $hash = Get-TestSha256 (Join-Path $stage ($artifact -replace '/', '\'))
                $evidence = Get-Content -LiteralPath (Join-Path $stage 'release-evidence.json') -Raw | ConvertFrom-Json
                $evidence.artifacts = @([ordered]@{ path = $artifact; type = 'exe'; sha256 = $hash })
                $evidence.signing.signed = @($artifact)
                Write-Utf8NoBom (Join-Path $stage 'release-evidence.json') ($evidence | ConvertTo-Json -Depth 10)
                Update-SyntheticStageInventory $stage
            }
            Pattern = 'subdirector|nested|top-level|allowlist'
        },
        @{
            Name = 'evidence-case-duplicate'
            Mutate = {
                param($stage)
                $evidencePath = Join-Path $stage 'release-evidence.json'
                $evidence = Get-Content -LiteralPath $evidencePath -Raw | ConvertFrom-Json
                $first = @($evidence.artifacts)[0]
                $evidence.artifacts = @(
                    [ordered]@{ path = $first.path; type = 'exe'; sha256 = $first.sha256 },
                    [ordered]@{ path = ([string]$first.path).ToUpperInvariant(); type = 'exe'; sha256 = $first.sha256 }
                )
                $evidence.signing.signed = @($first.path)
                Write-Utf8NoBom $evidencePath ($evidence | ConvertTo-Json -Depth 10)
                Update-SyntheticStageInventory $stage
            }
            Pattern = 'duplicate|case|artifact'
        }
    )

    $failures = @()
    foreach ($case in $cases) {
        $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName "deckpipe-stage-$($case.Name)"
        try {
            & $case.Mutate $stage
            $result = Invoke-SyntheticPassProbe $stage
            if ($result.status -eq 'PASS') {
                $failures += "accepted $($case.Name): $($result.message)"
            } elseif ($result.message -notmatch $case.Pattern) {
                $failures += "wrong message for $($case.Name): $($result.message)"
            }
        } finally {
            [IO.Directory]::Delete($stage, $true)
        }
    }
    if ($failures.Count -gt 0) { throw ($failures -join '; ') }
}

It 'rejects extra SPDX relationship types even when every required relationship is present' {
    $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-sbom-extra-relationship'
    try {
        $sbomPath = Join-Path $stage 'sbom.spdx.json'
        $sbom = Get-Content -LiteralPath $sbomPath -Raw | ConvertFrom-Json
        $artifactFile = @($sbom.files | Where-Object { $_.fileName -match '\.exe$' })[0]
        $relationships = @($sbom.relationships)
        $relationships += [pscustomobject]@{
            spdxElementId = 'SPDXRef-Package-DeckPipe'
            relationshipType = 'DEPENDS_ON'
            relatedSpdxElement = $artifactFile.SPDXID
        }
        $sbom.relationships = $relationships
        Write-Utf8NoBom $sbomPath ($sbom | ConvertTo-Json -Depth 10)
        $manifest = Get-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.txt')
        $manifest = $manifest -replace '^[0-9a-f]{64}  sbom\.spdx\.json$', "$(Get-TestSha256 $sbomPath)  sbom.spdx.json"
        Write-Utf8NoBom (Join-Path $stage 'SHA256SUMS.txt') (($manifest -join "`n") + "`n")

        $result = Invoke-SyntheticPassProbe $stage
        Assert-Equal $result.status 'FAIL' "Verifier must reject extra SBOM relationships, got $($result.status): $($result.message)"
        Assert-True ($result.message -match 'SPDX|relationship|extra|DEPENDS_ON') "Expected SBOM relationship failure, got $($result.message)"
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

It 'rejects ADS-like release paths and manifest tampering' {
    . (Join-Path $repoRoot 'release\verify.ps1')
    $failures = @()
    try {
        Get-ReleaseRelativePath 'DeckPipe-0.6.0:evil.exe' | Out-Null
        $failures += 'accepted ADS-like colon path'
    } catch {
        if ($_.Exception.Message -notmatch 'ADS|alternate|colon|escape') { $failures += "wrong ADS message: $($_.Exception.Message)" }
    }

    $stage = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-manifest-tamper'
    try {
        $manifest = Get-Content -LiteralPath (Join-Path $stage 'SHA256SUMS.txt')
        $manifest = $manifest -replace '^[0-9a-f]{64}  DeckPipe-', ('0' * 64 + '  DeckPipe-')
        Write-Utf8NoBom (Join-Path $stage 'SHA256SUMS.txt') (($manifest -join "`n") + "`n")
        $result = Invoke-SyntheticPassProbe $stage
        if ($result.status -ne 'FAIL') { $failures += "manifest tamper returned $($result.status): $($result.message)" }
        if ($result.message -notmatch 'hash mismatch') { $failures += "wrong tamper message: $($result.message)" }
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
    if ($failures.Count -gt 0) { throw ($failures -join '; ') }
}

It 'validates the offline wheelhouse manifest before build planning' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $caseRoot = New-TestLabCaseRoot 'wheelhouse-contract'
    try {
        $stage = Join-Path $caseRoot 'staging\candidate'
        $wheelhouse = New-TestWheelhouse -Root $caseRoot
        $manifestRecord = Assert-WheelhouseReady -WheelhouseDirectory $wheelhouse -StagingDirectory $stage
        Assert-Equal $manifestRecord.Path (Join-Path $wheelhouse 'wheelhouse-manifest.json') 'Wheelhouse manifest path drift'
        Assert-True ($manifestRecord.Sha256 -match '^[0-9a-f]{64}$') 'Wheelhouse manifest hash must be lowercase SHA-256'

        $missing = New-TestWheelhouse -Root (Join-Path $caseRoot 'missing')
        Remove-Item -LiteralPath (Join-Path $missing 'alpha-1.0.0-py3-none-any.whl') -Force
        Assert-Throws { Assert-WheelhouseReady -WheelhouseDirectory $missing -StagingDirectory $stage } 'wheelhouse|manifest|missing|mismatch'

        $extra = New-TestWheelhouse -Root (Join-Path $caseRoot 'extra')
        Write-Utf8NoBom (Join-Path $extra 'charlie-3.0.0-py3-none-any.whl') 'unexpected wheel'
        Assert-Throws { Assert-WheelhouseReady -WheelhouseDirectory $extra -StagingDirectory $stage } 'wheelhouse|manifest|extra|mismatch'

        $renamed = New-TestWheelhouse -Root (Join-Path $caseRoot 'renamed')
        Rename-Item -LiteralPath (Join-Path $renamed 'alpha-1.0.0-py3-none-any.whl') -NewName 'alpha-1.0.0-renamed.whl'
        Assert-Throws { Assert-WheelhouseReady -WheelhouseDirectory $renamed -StagingDirectory $stage } 'wheelhouse|manifest|missing|extra|mismatch'

        $tampered = New-TestWheelhouse -Root (Join-Path $caseRoot 'tampered')
        Write-Utf8NoBom (Join-Path $tampered 'alpha-1.0.0-py3-none-any.whl') 'tampered wheel'
        Assert-Throws { Assert-WheelhouseReady -WheelhouseDirectory $tampered -StagingDirectory $stage } 'wheelhouse|hash|mismatch'

        Assert-Throws { Assert-WheelhouseReady -WheelhouseDirectory $repoRoot -StagingDirectory $stage } 'wheelhouse|repository'
        $stagingWheelhouse = Join-Path $stage 'wheelhouse'
        [IO.Directory]::CreateDirectory($stagingWheelhouse) | Out-Null
        Assert-Throws { Assert-WheelhouseReady -WheelhouseDirectory $stagingWheelhouse -StagingDirectory $stage } 'wheelhouse|staging'
    } finally {
        if (Test-Path -LiteralPath $caseRoot) { [IO.Directory]::Delete($caseRoot, $true) }
    }
}

It 'records the prepared offline proof venv at the Task 11 tool-cache path' {
    $proofPython = 'D:\DeckPipe-RC-Lab\tool-cache\offline-proof\Scripts\python.exe'
    $proofEvidence = 'D:\DeckPipe-RC-Lab\qa-evidence\offline-install-tool-cache.json'
    Assert-True (Test-Path -LiteralPath $proofPython -PathType Leaf) 'Prepared offline proof Python is missing from D:\DeckPipe-RC-Lab\tool-cache\offline-proof'
    Assert-True (Test-Path -LiteralPath $proofEvidence -PathType Leaf) 'Prepared offline proof evidence is missing from qa-evidence'
    $evidence = Get-Content -LiteralPath $proofEvidence -Raw | ConvertFrom-Json
    Assert-Equal $evidence.offline_venv 'D:\DeckPipe-RC-Lab\tool-cache\offline-proof' 'Offline proof evidence must bind the Task 11 venv path'
    Assert-Equal $evidence.offline_python $proofPython 'Offline proof evidence must bind the Task 11 Python path'
    Assert-True ([string]$evidence.pip_check_summary -match 'No broken requirements found') 'Offline proof evidence must record pip check success'
}

It 'fails build planning before dependency work when PythonExe is not existing CPython 3.12 x64' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $caseRoot = New-TestLabCaseRoot 'python-precondition'
    try {
        $stage = Join-Path $caseRoot 'stage'
        $wheelhouse = New-TestWheelhouse -Root $caseRoot
        $version = Read-JsonFile 'release\version.json'
        $sourceRevision = 'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'
        Assert-Throws {
            New-ReleaseBuildPlan -StagingDirectory $stage -WheelhouseDirectory $wheelhouse -Version $version -SourceRevision $sourceRevision -PythonExe (Join-Path $caseRoot 'missing-python.exe')
        } 'PythonExe|CPython|3\.12|x64|AMD64|existing'
    } finally {
        if (Test-Path -LiteralPath $caseRoot) { [IO.Directory]::Delete($caseRoot, $true) }
    }
}

It 'plans release builds only from an isolated tracked HEAD temp workspace' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $tempRoot = New-TestLabCaseRoot 'deckpipe-build-plan'
    $stage = Join-Path $tempRoot 'stage'
    $wheelhouse = New-TestWheelhouse -Root $tempRoot
    try {
        $version = Read-JsonFile 'release\version.json'
        $sourceRevision = 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        Assert-Throws { New-ReleaseBuildPlan -StagingDirectory $stage -Version $version -SourceRevision $sourceRevision -PythonExe 'python.exe' } 'wheelhouse'
        $plan = New-ReleaseBuildPlan -StagingDirectory $stage -WheelhouseDirectory $wheelhouse -Version $version -SourceRevision $sourceRevision -PythonExe (Get-ReleaseTestPythonExe)

        $repoFull = [IO.Path]::GetFullPath($repoRoot).TrimEnd('\')
        $stageFull = [IO.Path]::GetFullPath($stage).TrimEnd('\')
        foreach ($path in @($plan.BuildRoot, $plan.SourceRoot, $plan.PyInstallerDistPath, $plan.PyInstallerWorkPath, $plan.PyInstallerSpecPath, $plan.CargoTargetDir)) {
            $full = [IO.Path]::GetFullPath([string]$path).TrimEnd('\')
            Assert-False ($full.StartsWith($repoFull, [StringComparison]::OrdinalIgnoreCase)) "Build path leaks into repo: $full"
            Assert-False ($full.StartsWith($stageFull, [StringComparison]::OrdinalIgnoreCase)) "Build path leaks into staging: $full"
            Assert-True ($full.StartsWith(([IO.Path]::GetFullPath('D:\DeckPipe-RC-Lab\build').TrimEnd('\')), [StringComparison]::OrdinalIgnoreCase)) "Build path is not lab-owned: $full"
        }

        Assert-Equal $plan.WheelhouseManifestPath (Join-Path $wheelhouse 'wheelhouse-manifest.json') 'Wheelhouse manifest path missing from plan'
        Assert-True ($plan.WheelhouseManifestSha256 -match '^[0-9a-f]{64}$') 'Wheelhouse manifest hash missing from plan'
        Assert-Equal @($plan.PipInstallCommands).Count 2 'Both runtime and build locks must be installed'
        $pipText = ((@($plan.PipInstallCommands) | ForEach-Object { $_.Arguments -join ' ' }) -join "`n")
        Assert-True ($pipText -match [regex]::Escape('requirements-build.lock')) 'Build lock install missing'
        Assert-True ($pipText -match [regex]::Escape('requirements.lock')) 'Runtime lock install missing'
        Assert-True ($pipText -match '--no-index') 'pip must be offline'
        Assert-True ($pipText -match '--require-hashes') 'pip must enforce hashes'
        Assert-True ($pipText -match '--no-cache-dir') 'pip must not read a global cache during build installs'
        Assert-True ($pipText -match [regex]::Escape($wheelhouse)) 'pip must use explicit wheelhouse'
        Assert-True ((@($plan.PyInstallerArguments) -join ' ') -match '--distpath') 'PyInstaller distpath must be controlled'
        Assert-True ((@($plan.PyInstallerArguments) -join ' ') -match '--workpath') 'PyInstaller workpath must be controlled'
        Assert-True ((@($plan.PyInstallerArguments) -join ' ') -match '--specpath') 'PyInstaller specpath must be controlled'
        $addDataIndex = [array]::IndexOf([object[]]$plan.PyInstallerArguments, '--add-data')
        Assert-True ($addDataIndex -ge 0 -and $addDataIndex -lt (@($plan.PyInstallerArguments).Count - 1)) 'PyInstaller static asset add-data argument is missing'
        $addData = [string]$plan.PyInstallerArguments[$addDataIndex + 1]
        $addDataParts = @($addData -split ';', 2)
        Assert-Equal $addDataParts.Count 2 'PyInstaller add-data must contain one source and one destination'
        Assert-Equal $addDataParts[1] 'app/static' 'PyInstaller static asset destination drift'
        $staticAssetSource = [IO.Path]::GetFullPath($addDataParts[0]).TrimEnd('\')
        Assert-True ([IO.Path]::IsPathRooted($addDataParts[0])) 'PyInstaller static asset source must be absolute so specpath cannot change resolution'
        Assert-True ($staticAssetSource.StartsWith(([IO.Path]::GetFullPath($plan.SourceRoot).TrimEnd('\') + '\'), [StringComparison]::OrdinalIgnoreCase)) "PyInstaller static asset source must come from tracked temp source: $staticAssetSource"
        Assert-True ($plan.ExpectedArtifactNames -contains 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-aaaaaaa-x64.exe') 'Expected application artifact name missing'
        Assert-True ($plan.ExpectedArtifactNames -contains 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-aaaaaaa-x64-setup.exe') 'Expected setup artifact name missing'
        Assert-True ($plan.ExpectedArtifactNames -contains 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-aaaaaaa-x64.msi') 'Expected MSI artifact name missing'
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { [IO.Directory]::Delete($tempRoot, $true) }
    }
}

It 'plans private beta artifacts with explicit unsigned-private-beta naming and rejects mixed signing modes' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $tempRoot = New-TestLabCaseRoot 'deckpipe-private-beta-plan'
    $stage = Join-Path $tempRoot 'stage'
    $wheelhouse = New-TestWheelhouse -Root $tempRoot
    try {
        $version = Read-JsonFile 'release\version.json'
        $sourceRevision = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
        $plan = New-ReleaseBuildPlan -StagingDirectory $stage -WheelhouseDirectory $wheelhouse -Version $version -SourceRevision $sourceRevision -PythonExe (Get-ReleaseTestPythonExe) -PrivateBetaCandidate
        Assert-True ($plan.ExpectedArtifactNames -contains 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-bbbbbbb-unsigned-private-beta-x64.exe') 'Private beta application artifact name missing'
        Assert-True ($plan.ExpectedArtifactNames -contains 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-bbbbbbb-unsigned-private-beta-x64-setup.exe') 'Private beta setup artifact name missing'
        Assert-True ($plan.ExpectedArtifactNames -contains 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-bbbbbbb-unsigned-private-beta-x64.msi') 'Private beta MSI artifact name missing'

        Assert-Throws {
            Invoke-ReleaseBuild -StagingDirectory $stage -UnsignedEngineeringCandidate -PrivateBetaCandidate
        } 'PrivateBetaCandidate|UnsignedEngineeringCandidate|cannot combine'
        Assert-Throws {
            Invoke-ReleaseBuild -StagingDirectory $stage -PrivateBetaCandidate -SignToolPath 'signtool.exe'
        } 'PrivateBetaCandidate|signing|cannot combine'
        Assert-Throws {
            Invoke-ReleaseBuild -StagingDirectory $stage -PrivateBetaCandidate -SigningCertificateThumbprint '001122'
        } 'PrivateBetaCandidate|signing|cannot combine'
        Assert-Throws {
            Invoke-ReleaseBuild -StagingDirectory $stage -PrivateBetaCandidate -TimestampUrl 'https://timestamp.example/rfc3161'
        } 'PrivateBetaCandidate|timestamp|cannot combine'
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { [IO.Directory]::Delete($tempRoot, $true) }
    }
}

It 'returns the published final staging path after a successful candidate transaction' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-transaction-return-' + [guid]::NewGuid().ToString('N'))
    $stage = Join-Path $tempRoot 'final-stage'
    [IO.Directory]::CreateDirectory($tempRoot) | Out-Null
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    try {
        $version = Read-JsonFile 'release\version.json'
        $sourceRevision = Get-TestCurrentHead
        $publishedStage = Invoke-ReleaseCandidateTransaction -StagingDirectory $stage -Version $version -SourceRevision $sourceRevision -PrivateBetaCandidate -AssembleCandidate {
            param($CandidateDirectory, $ExpectedArtifactNames)
            Write-Output ''
            Write-Output 'native build chatter before publish'
            $fixture = New-SyntheticReleaseStage -WithExecutable -SignedEvidence -StageName 'deckpipe-transaction-return-fixture' -BuildId $version.build_id -SourceRevision $sourceRevision
            try {
                Set-SyntheticPrivateBetaPolicy -StagePath $fixture
                foreach ($file in @(Get-ChildItem -LiteralPath $fixture -Force -File)) {
                    Copy-Item -LiteralPath $file.FullName -Destination (Join-Path $CandidateDirectory $file.Name)
                }
            } finally {
                if (Test-Path -LiteralPath $fixture) { [IO.Directory]::Delete($fixture, $true) }
            }
        } -ValidateCandidate {
            param($CandidateDirectory)
            $result = Invoke-ReleaseVerifierForCandidate -CandidateDirectory $CandidateDirectory -PrivateBetaCandidate
            Assert-Equal $result.status 'PASS' 'Candidate transaction fixture must verify before publish'
            Write-Output 'verifier chatter before publish'
        }
        Assert-Equal $publishedStage ([IO.Path]::GetFullPath($stage).TrimEnd('\')) 'Successful transaction must return the final staging path'
        Assert-True (Test-Path -LiteralPath $stage -PathType Container) 'Successful transaction must publish final staging'
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { [IO.Directory]::Delete($tempRoot, $true) }
    }
}

It 'keeps final staging empty and cleans the owned candidate when candidate validation fails' {
    . (Join-Path $repoRoot 'release\build.ps1')
    $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-transaction-' + [guid]::NewGuid().ToString('N'))
    $stage = Join-Path $tempRoot 'final-stage'
    $observedCandidate = $null
    [IO.Directory]::CreateDirectory($tempRoot) | Out-Null
    try {
        $version = Read-JsonFile 'release\version.json'
        $sourceRevision = 'cccccccccccccccccccccccccccccccccccccccc'
        Assert-Throws {
            Invoke-ReleaseCandidateTransaction -StagingDirectory $stage -Version $version -SourceRevision $sourceRevision -UnsignedEngineeringCandidate -AssembleCandidate {
                param($CandidateDirectory, $ExpectedArtifactNames)
                $script:ObservedCandidateForTransactionTest = $CandidateDirectory
                foreach ($name in @($ExpectedArtifactNames)) {
                    Write-Utf8NoBom (Join-Path $CandidateDirectory $name) 'candidate bytes'
                }
            } -ValidateCandidate {
                param($CandidateDirectory)
                throw 'simulated candidate validation failure'
            }
        } 'simulated candidate validation failure'
        $observedCandidate = $script:ObservedCandidateForTransactionTest
        Assert-False (Test-Path -LiteralPath $stage) 'Failed candidate must not publish the final staging directory'
        Assert-True ($observedCandidate -and -not (Test-Path -LiteralPath $observedCandidate)) 'Owned candidate directory must be cleaned after failure'
    } finally {
        if (Test-Path -LiteralPath $tempRoot) { [IO.Directory]::Delete($tempRoot, $true) }
        $script:ObservedCandidateForTransactionTest = $null
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
        } -ExpectedSourceRevision (Get-TestCurrentHead)
        Assert-Equal $result.status 'PASS' "PASS fixture failed: $($result.message)"
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

It 'generates deterministic SPDX 2.3 with unique ids checksums and package contains relationships' {
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-sbom-' + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    try {
        Write-Utf8NoBom (Join-Path $stage 'release-evidence.json') '{"schema_version":1}'
        Write-Utf8NoBom (Join-Path $stage 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-09767c4-x64.exe') 'application bytes'
        $out1 = Join-Path $stage 'sbom1.spdx.json'
        $out2 = Join-Path $stage 'sbom2.spdx.json'
        & (Join-Path $repoRoot 'release\New-SpdxSbom.ps1') -InputDirectory $stage -OutputPath $out1 -VersionJsonPath (Join-Path $repoRoot 'release\version.json')
        $sbom1Text = Get-Content -LiteralPath $out1 -Raw
        Remove-Item -LiteralPath $out1 -Force
        & (Join-Path $repoRoot 'release\New-SpdxSbom.ps1') -InputDirectory $stage -OutputPath $out2 -VersionJsonPath (Join-Path $repoRoot 'release\version.json')
        $sbom2Text = Get-Content -LiteralPath $out2 -Raw
        Assert-Equal $sbom2Text $sbom1Text 'SBOM output must be deterministic'
        $sbom = $sbom2Text | ConvertFrom-Json
        Remove-Item -LiteralPath $out2 -Force
        Assert-Equal $sbom.spdxVersion 'SPDX-2.3'
        $fileIds = @($sbom.files | ForEach-Object { $_.SPDXID })
        Assert-Equal @($fileIds | Select-Object -Unique).Count $fileIds.Count 'SPDX file IDs must be unique'
        foreach ($relative in @('release-evidence.json', 'DeckPipe-0.6.0+20260827.050713.6456dba254a6-09767c4-x64.exe')) {
            $file = @($sbom.files | Where-Object { $_.fileName -eq $relative })[0]
            Assert-True ($null -ne $file) "Missing SBOM file $relative"
            $expectedHash = Get-TestSha256 (Join-Path $stage ($relative -replace '/', '\'))
            Assert-Equal $file.checksums[0].checksumValue $expectedHash "Checksum mismatch for $relative"
            $contains = @($sbom.relationships | Where-Object { $_.spdxElementId -eq 'SPDXRef-Package-DeckPipe' -and $_.relationshipType -eq 'CONTAINS' -and $_.relatedSpdxElement -eq $file.SPDXID })
            Assert-Equal $contains.Count 1 "Missing package CONTAINS relationship for $relative"
        }
        Write-Utf8NoBom (Join-Path $stage 'extra.txt') 'extra'
        Assert-Throws { & (Join-Path $repoRoot 'release\New-SpdxSbom.ps1') -InputDirectory $stage -OutputPath (Join-Path $stage 'blocked.spdx.json') -VersionJsonPath (Join-Path $repoRoot 'release\version.json') } 'allowlist|unexpected'
        Remove-Item -LiteralPath (Join-Path $stage 'extra.txt') -Force
        [IO.Directory]::CreateDirectory((Join-Path $stage 'nested')) | Out-Null
        Assert-Throws { & (Join-Path $repoRoot 'release\New-SpdxSbom.ps1') -InputDirectory $stage -OutputPath (Join-Path $stage 'blocked.spdx.json') -VersionJsonPath (Join-Path $repoRoot 'release\version.json') } 'nested'
    } finally {
        [IO.Directory]::Delete($stage, $true)
    }
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
