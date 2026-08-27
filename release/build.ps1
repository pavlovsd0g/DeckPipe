param(
    [string]$StagingDirectory,
    [switch]$UnsignedEngineeringCandidate,
    [string]$SignToolPath,
    [string]$SigningCertificateThumbprint,
    [string]$TimestampUrl,
    [string]$PythonExe = 'python.exe',
    [string]$WheelhouseDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path

function Read-JsonFile {
    param([string]$RelativePath)
    $path = Join-Path $script:RepoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path)) { throw "missing release input: $RelativePath" }
    return Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
}

function Read-TextFile {
    param([string]$RelativePath)
    $path = Join-Path $script:RepoRoot $RelativePath
    if (-not (Test-Path -LiteralPath $path)) { throw "missing release input: $RelativePath" }
    return Get-Content -LiteralPath $path -Raw
}

function Write-Utf8NoBom {
    param([string]$Path, [string]$Text)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $Path)) | Out-Null
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-Sha256 {
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

function Get-LockRootVersion {
    param([string]$RelativePath)
    $text = Read-TextFile $RelativePath
    if ($text -notmatch '(?s)"packages"\s*:\s*\{\s*""\s*:\s*\{.*?"version"\s*:\s*"([^"]+)"') {
        throw "version drift: cannot read root package version from $RelativePath"
    }
    return $Matches[1]
}

function Get-LockTopVersion {
    param([string]$RelativePath)
    $text = Read-TextFile $RelativePath
    if ($text -notmatch '(?m)^\s*"version"\s*:\s*"([^"]+)"\s*,') {
        throw "version drift: cannot read top package-lock version from $RelativePath"
    }
    return $Matches[1]
}

function Assert-VersionSync {
    $version = Read-JsonFile 'release\version.json'
    if ($version.version -ne '0.6.0') { throw 'version drift: release/version.json' }
    if ($version.build_id -notmatch '^0\.6\.0\+[0-9]{8}\.[0-9]{6}\.[0-9a-f]{7,40}$') {
        throw 'version drift: invalid opaque build_id'
    }
    if ($version.PSObject.Properties.Name -contains 'source_revision') {
        throw 'version drift: release/version.json must not embed source revision provenance'
    }
    if ($version.artifact_name_prefix -ne "DeckPipe-$($version.build_id)") {
        throw 'version drift: invalid artifact_name_prefix'
    }

    $rootPackage = Read-JsonFile 'package.json'
    $desktopPackage = Read-JsonFile 'desktop\package.json'
    $tauri = Read-JsonFile 'desktop\src-tauri\tauri.conf.json'
    $cargoToml = Read-TextFile 'desktop\src-tauri\Cargo.toml'

    if ($rootPackage.version -ne $version.version) { throw 'version drift: package.json' }
    if ((Get-LockTopVersion 'package-lock.json') -ne $version.version -or (Get-LockRootVersion 'package-lock.json') -ne $version.version) { throw 'version drift: package-lock.json' }
    if ($desktopPackage.version -ne $version.version) { throw 'version drift: desktop/package.json' }
    if ((Get-LockTopVersion 'desktop\package-lock.json') -ne $version.version -or (Get-LockRootVersion 'desktop\package-lock.json') -ne $version.version) { throw 'version drift: desktop/package-lock.json' }
    if ($tauri.version -ne $version.version) { throw 'version drift: desktop/src-tauri/tauri.conf.json' }
    if ($cargoToml -notmatch '(?m)^version = "0\.6\.0"$') { throw 'version drift: desktop/src-tauri/Cargo.toml' }
    return $version
}

function Assert-PythonLocksReady {
    foreach ($relative in @('requirements.lock', 'requirements-build.lock')) {
        $text = Read-TextFile $relative
        if ($text -match '(?m)^# LOCK-STATUS: BLOCKED$') {
            throw "Python lock BLOCKED: $relative lacks hash-complete offline distribution evidence"
        }
        if ($text -notmatch '(?m)^--require-hashes$') {
            throw "Python lock missing --require-hashes: $relative"
        }
        if ($text -notmatch '--hash=sha256:[0-9a-f]{64}') {
            throw "Python lock missing real hashes: $relative"
        }
    }
}

function Assert-NoForbiddenPath {
    param([string]$RelativePath)
    $normalized = ($RelativePath -replace '\\', '/')
    if ($normalized -match '(^|/)(config\.local\.json|cookies?\.txt|master\.db)$') { throw "forbidden release input: $RelativePath" }
    if ($normalized -match '(?i)(credential|secret|token|cookie|profile|appdata|localappdata|rekordbox|master\.db|\.sqlite|\.db$|\.media$)') {
        throw "forbidden release input: $RelativePath"
    }
}

function Assert-StagingDirectorySafe {
    param([string]$Path)
    if (-not $Path) { throw 'StagingDirectory is required' }
    $full = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)
    $repoFull = [IO.Path]::GetFullPath($script:RepoRoot).TrimEnd('\')
    $stageFull = [IO.Path]::GetFullPath($full).TrimEnd('\')
    if ($stageFull -ieq $repoFull) { throw 'staging directory cannot be the repository root' }

    if (Test-Path -LiteralPath $stageFull) {
        $item = Get-Item -LiteralPath $stageFull -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'staging directory reparse point is not allowed' }
        if (-not $item.PSIsContainer) { throw 'staging path must be a directory' }
        $entries = @(Get-ChildItem -LiteralPath $stageFull -Force)
        if ($entries.Count -gt 0) { throw 'staging directory is nonempty; remove unexpected stale entries before release build' }
    } else {
        $parent = Split-Path -Parent $stageFull
        if (-not $parent -or -not (Test-Path -LiteralPath $parent)) { throw 'staging directory parent is missing' }
        $parentItem = Get-Item -LiteralPath $parent -Force
        if (($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'staging directory parent reparse point is not allowed' }
    }
    return $stageFull
}

function Get-SourceRevision {
    $revision = (& git -C $script:RepoRoot rev-parse HEAD 2>$null | Select-Object -First 1)
    if (-not $revision -or $LASTEXITCODE -ne 0) { throw 'source provenance BLOCKED: git rev-parse HEAD failed' }
    $dirty = (& git -C $script:RepoRoot status --porcelain --untracked-files=no 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'source provenance BLOCKED: git status failed' }
    if (@($dirty).Count -gt 0) { throw 'source provenance BLOCKED: tracked source is dirty' }
    return [string]$revision
}

function Get-ExpectedArtifactNames {
    param($Version, [string]$SourceRevision)
    $short = $SourceRevision.Substring(0, 7)
    return @(
        "$($Version.artifact_name_prefix)-$short-x64.exe",
        "$($Version.artifact_name_prefix)-$short-x64-setup.exe",
        "$($Version.artifact_name_prefix)-$short-x64.msi"
    )
}

function Assert-PathNotInside {
    param(
        [string]$Path,
        [string]$ForbiddenRoot,
        [string]$Message
    )
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $root = [IO.Path]::GetFullPath($ForbiddenRoot).TrimEnd('\')
    if ($full -ieq $root -or $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw $Message
    }
}

function New-ReleaseBuildPlan {
    param(
        [string]$StagingDirectory,
        [string]$WheelhouseDirectory,
        $Version,
        [string]$SourceRevision,
        [string]$PythonExe = 'python.exe'
    )
    if (-not $WheelhouseDirectory) { throw 'dependency BLOCKED: explicit offline wheelhouse is required' }
    if (-not (Test-Path -LiteralPath $WheelhouseDirectory -PathType Container)) { throw 'dependency BLOCKED: offline wheelhouse is missing' }
    if (-not $SourceRevision -or $SourceRevision -notmatch '^[0-9a-f]{40}$') { throw 'source provenance BLOCKED: exact 40-character tracked HEAD is required' }

    $stagePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StagingDirectory)
    $repoFull = [IO.Path]::GetFullPath($script:RepoRoot)
    $stageFull = [IO.Path]::GetFullPath($stagePath)
    $tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    $buildRoot = Join-Path $tempBase ("deckpipe-release-build-" + [guid]::NewGuid().ToString('N'))
    Assert-PathNotInside -Path $buildRoot -ForbiddenRoot $repoFull -Message 'build workspace must be outside the repository'
    Assert-PathNotInside -Path $buildRoot -ForbiddenRoot $stageFull -Message 'build workspace must be outside staging'

    $sourceRoot = Join-Path $buildRoot 'source'
    $venvPath = Join-Path $buildRoot 'venv'
    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    $distPath = Join-Path $buildRoot 'pyinstaller-dist'
    $workPath = Join-Path $buildRoot 'pyinstaller-work'
    $specPath = Join-Path $buildRoot 'pyinstaller-spec'
    $cargoTarget = Join-Path $buildRoot 'cargo-target'
    $bundleRoot = Join-Path $cargoTarget 'release\bundle'
    $wheelhouseFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WheelhouseDirectory)

    $pipInstallCommands = @(
        [pscustomobject]@{ Arguments = @('-m', 'pip', 'install', '--no-index', '--require-hashes', '--find-links', $wheelhouseFull, '-r', (Join-Path $sourceRoot 'requirements-build.lock')) },
        [pscustomobject]@{ Arguments = @('-m', 'pip', 'install', '--no-index', '--require-hashes', '--find-links', $wheelhouseFull, '-r', (Join-Path $sourceRoot 'requirements.lock')) }
    )
    $pyInstallerArguments = @(
        '-m', 'PyInstaller', '--noconfirm', '--onefile', '--name', 'deckpipe-backend',
        '--distpath', $distPath, '--workpath', $workPath, '--specpath', $specPath,
        '--collect-all', 'yt_dlp', '--collect-all', 'deezer_python_gql', '--collect-all', 'imageio_ffmpeg',
        '--collect-all', 'uvicorn', '--collect-all', 'sqlcipher3',
        '--add-data', 'app/static;app/static',
        '--hidden-import', 'pyrekordbox', '--hidden-import', 'mutagen', '--hidden-import', 'Crypto',
        '--hidden-import', 'app.main', '--hidden-import', 'app.jobs', '--hidden-import', 'app.library',
        '--hidden-import', 'app.deezer_client', '--hidden-import', 'app.soundcloud', '--hidden-import', 'app.tagger',
        '--hidden-import', 'app.converter', '--hidden-import', 'app.bugreport', '--hidden-import', 'app.rekordbox',
        '--hidden-import', 'uvicorn.loops.auto', '--hidden-import', 'uvicorn.protocols.http.auto',
        '--hidden-import', 'uvicorn.protocols.websockets.auto', '--hidden-import', 'uvicorn.lifespan.on',
        (Join-Path $sourceRoot 'run_backend.py')
    )

    return [pscustomobject]@{
        BuildRoot = $buildRoot
        SourceRoot = $sourceRoot
        VenvPath = $venvPath
        VenvPython = $venvPython
        PyInstallerDistPath = $distPath
        PyInstallerWorkPath = $workPath
        PyInstallerSpecPath = $specPath
        PyInstallerArguments = $pyInstallerArguments
        PipInstallCommands = $pipInstallCommands
        CargoTargetDir = $cargoTarget
        BundleRoot = $bundleRoot
        ExpectedArtifactNames = @(Get-ExpectedArtifactNames -Version $Version -SourceRevision $SourceRevision)
        ApplicationArtifactName = "$($Version.artifact_name_prefix)-$($SourceRevision.Substring(0, 7))-x64.exe"
        SetupArtifactName = "$($Version.artifact_name_prefix)-$($SourceRevision.Substring(0, 7))-x64-setup.exe"
        MsiArtifactName = "$($Version.artifact_name_prefix)-$($SourceRevision.Substring(0, 7))-x64.msi"
        PythonExe = $PythonExe
    }
}

function Get-ReleaseArtifacts {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory)) { return @() }
    return @(Get-ChildItem -LiteralPath $Directory -Recurse -Force -File |
        Where-Object { $_.Extension -in @('.exe', '.msi') } |
        Sort-Object FullName)
}

function Get-TopLevelReleaseArtifacts {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory)) { return @() }
    return @(Get-ChildItem -LiteralPath $Directory -Force -File |
        Where-Object { $_.Extension -in @('.exe', '.msi') } |
        Sort-Object Name)
}

function Remove-OwnedBuildRoot {
    param([string]$BuildRoot, [string]$StagePath)
    if (-not $BuildRoot -or -not (Test-Path -LiteralPath $BuildRoot)) { return }
    Assert-PathNotInside -Path $BuildRoot -ForbiddenRoot $script:RepoRoot -Message 'refusing to remove a repository path as build workspace'
    Assert-PathNotInside -Path $BuildRoot -ForbiddenRoot $StagePath -Message 'refusing to remove a staging path as build workspace'
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}

function Export-TrackedSourceToTemp {
    param($Plan)
    [IO.Directory]::CreateDirectory($Plan.SourceRoot) | Out-Null
    $archivePath = Join-Path $Plan.BuildRoot 'source.tar'
    & git -C $script:RepoRoot archive --format=tar HEAD -o $archivePath
    if ($LASTEXITCODE -ne 0) { throw 'source provenance BLOCKED: git archive HEAD failed' }
    & tar -xf $archivePath -C $Plan.SourceRoot
    if ($LASTEXITCODE -ne 0) { throw 'source provenance BLOCKED: extracting tracked HEAD archive failed' }
}

function Write-Sha256Manifest {
    param([string]$Directory)
    $lines = @()
    $seen = @{}
    foreach ($entry in @(Get-ChildItem -LiteralPath $Directory -Force | Where-Object { $_.PSIsContainer })) {
        throw "unexpected staging subdirectory before manifest: $($entry.Name)"
    }
    $files = @(Get-ChildItem -LiteralPath $Directory -Force -File |
        Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
        Sort-Object Name)
    foreach ($file in $files) {
        $relative = $file.FullName.Substring($Directory.Length).TrimStart('\') -replace '\\', '/'
        Assert-NoForbiddenPath $relative
        $key = $relative.ToLowerInvariant()
        if ($seen.ContainsKey($key)) { throw "duplicate manifest path: $relative" }
        $seen[$key] = $true
        $lines += "$(Get-Sha256 $file.FullName)  $relative"
    }
    Write-Utf8NoBom (Join-Path $Directory 'SHA256SUMS.txt') (($lines -join "`n") + "`n")
}

function New-SignToolArguments {
    param(
        [string]$CertificateThumbprint,
        [string]$TimestampUrl,
        [string]$ArtifactPath
    )
    if (-not $CertificateThumbprint -or $CertificateThumbprint -notmatch '^[0-9A-Fa-f]+$') { throw 'signing BLOCKED: exact certificate thumbprint is required' }
    if (-not $TimestampUrl -or $TimestampUrl -notmatch '^https://') { throw 'timestamp BLOCKED: RFC3161 https TimestampUrl is required' }
    if (-not $ArtifactPath) { throw 'signing BLOCKED: artifact path is required' }
    return @('sign', '/fd', 'SHA256', '/sha1', $CertificateThumbprint, '/tr', $TimestampUrl, '/td', 'SHA256', $ArtifactPath)
}

function Invoke-ArtifactSigning {
    param(
        [string]$SignToolPath,
        [string[]]$Artifacts,
        [string]$CertificateThumbprint,
        [string]$TimestampUrl,
        [scriptblock]$SignExecutor
    )
    if (-not $SignToolPath) { throw 'signing BLOCKED: SignToolPath is required' }
    if ($null -eq $SignExecutor -and -not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) { throw 'signing BLOCKED: SignToolPath does not exist' }
    if ($null -eq $SignExecutor) {
        $SignExecutor = {
            param($Tool, $Arguments)
            & $Tool @Arguments
            return $LASTEXITCODE
        }
    }
    foreach ($artifact in $Artifacts) {
        $args = New-SignToolArguments -CertificateThumbprint $CertificateThumbprint -TimestampUrl $TimestampUrl -ArtifactPath $artifact
        $code = & $SignExecutor $SignToolPath $args
        if ($code -ne 0) { throw "signtool failed for $(Split-Path -Leaf $artifact)" }
        if (Test-Path -LiteralPath $artifact -PathType Leaf) {
            $signature = Get-AuthenticodeSignature -LiteralPath $artifact
            if ($signature.Status -ne 'Valid') { throw "signtool verification failed for $(Split-Path -Leaf $artifact)" }
            if ($null -eq $signature.TimeStamperCertificate) { throw "timestamp verification failed for $(Split-Path -Leaf $artifact)" }
        }
    }
}

function Invoke-ReleaseBuild {
    param(
        [string]$StagingDirectory,
        [switch]$UnsignedEngineeringCandidate,
        [string]$SignToolPath,
        [string]$SigningCertificateThumbprint,
        [string]$TimestampUrl,
        [string]$PythonExe = 'python.exe',
        [string]$WheelhouseDirectory
    )

    $plan = $null
    $stagePath = Assert-StagingDirectorySafe $StagingDirectory
    $version = Assert-VersionSync
    Assert-PythonLocksReady
    $sourceRevision = Get-SourceRevision

    if (-not $UnsignedEngineeringCandidate) {
        if (-not $SignToolPath) { throw 'signing BLOCKED: SignToolPath is required' }
        if (-not $SigningCertificateThumbprint) { throw 'signing BLOCKED: exact certificate thumbprint is required' }
        if (-not $TimestampUrl) { throw 'timestamp BLOCKED: RFC3161 TimestampUrl is required' }
    }

    $plan = New-ReleaseBuildPlan -StagingDirectory $stagePath -WheelhouseDirectory $WheelhouseDirectory -Version $version -SourceRevision $sourceRevision -PythonExe $PythonExe
    [IO.Directory]::CreateDirectory($stagePath) | Out-Null
    [IO.Directory]::CreateDirectory($plan.BuildRoot) | Out-Null
    $oldCargoTargetDir = $env:CARGO_TARGET_DIR
    $oldCargoNetOffline = $env:CARGO_NET_OFFLINE
    try {
        Export-TrackedSourceToTemp -Plan $plan
        & $PythonExe -m venv $plan.VenvPath
        if ($LASTEXITCODE -ne 0) { throw 'build failed: Python venv creation failed' }
        foreach ($pipCommand in @($plan.PipInstallCommands)) {
            & $plan.VenvPython @($pipCommand.Arguments)
            if ($LASTEXITCODE -ne 0) { throw 'build failed: offline Python dependency install failed' }
        }

        Push-Location $plan.SourceRoot
        try {
            & $plan.VenvPython @($plan.PyInstallerArguments)
            if ($LASTEXITCODE -ne 0) { throw 'build failed: PyInstaller backend build failed' }
        } finally {
            Pop-Location
        }

        $sidecarDir = Join-Path $plan.SourceRoot 'desktop\src-tauri\binaries'
        [IO.Directory]::CreateDirectory($sidecarDir) | Out-Null
        Copy-Item -LiteralPath (Join-Path $plan.PyInstallerDistPath 'deckpipe-backend.exe') -Destination (Join-Path $sidecarDir 'deckpipe-backend-x86_64-pc-windows-msvc.exe') -Force

        Push-Location $plan.SourceRoot
        try {
            & npm ci --offline
            if ($LASTEXITCODE -ne 0) { throw 'build failed: root npm ci --offline failed' }
        } finally {
            Pop-Location
        }
        Push-Location (Join-Path $plan.SourceRoot 'desktop')
        try {
            & npm ci --offline
            if ($LASTEXITCODE -ne 0) { throw 'build failed: desktop npm ci --offline failed' }
            $env:CARGO_TARGET_DIR = $plan.CargoTargetDir
            $env:CARGO_NET_OFFLINE = 'true'
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw 'build failed: Tauri build failed' }
        } finally {
            Pop-Location
        }

        $applicationExe = Join-Path $plan.CargoTargetDir 'release\deckpipe.exe'
        if (-not (Test-Path -LiteralPath $applicationExe -PathType Leaf)) { throw 'build failed: application exe was not produced in temp cargo target' }
        Copy-Item -LiteralPath $applicationExe -Destination (Join-Path $stagePath $plan.ApplicationArtifactName)

        $builtArtifacts = Get-ReleaseArtifacts $plan.BundleRoot
        $setupArtifacts = @($builtArtifacts | Where-Object { $_.Name -match '(?i)setup\.exe$' })
        $msiArtifacts = @($builtArtifacts | Where-Object { $_.Extension -ieq '.msi' })
        if ($setupArtifacts.Count -ne 1) { throw 'build failed: expected exactly one setup executable artifact' }
        if ($msiArtifacts.Count -ne 1) { throw 'build failed: expected exactly one MSI artifact' }
        Copy-Item -LiteralPath $setupArtifacts[0].FullName -Destination (Join-Path $stagePath $plan.SetupArtifactName)
        Copy-Item -LiteralPath $msiArtifacts[0].FullName -Destination (Join-Path $stagePath $plan.MsiArtifactName)

        Remove-OwnedBuildRoot -BuildRoot $plan.BuildRoot -StagePath $stagePath

        $stagedArtifacts = Get-TopLevelReleaseArtifacts $stagePath
        $expectedByName = @{}
        foreach ($name in @($plan.ExpectedArtifactNames)) { $expectedByName[$name.ToLowerInvariant()] = $name }
        foreach ($artifact in $stagedArtifacts) {
            if (-not $expectedByName.ContainsKey($artifact.Name.ToLowerInvariant())) { throw "unexpected staged artifact: $($artifact.Name)" }
            $expectedByName.Remove($artifact.Name.ToLowerInvariant())
        }
        if ($expectedByName.Count -gt 0) { throw "build failed: missing expected artifact(s): $((@($expectedByName.Values) | Sort-Object) -join ', ')" }

        if (-not $UnsignedEngineeringCandidate) {
            Invoke-ArtifactSigning -SignToolPath $SignToolPath -Artifacts @($stagedArtifacts | ForEach-Object { $_.FullName }) -CertificateThumbprint $SigningCertificateThumbprint -TimestampUrl $TimestampUrl
        }

        $artifactRecords = @()
        foreach ($artifact in $stagedArtifacts) {
            $relative = $artifact.FullName.Substring($stagePath.Length).TrimStart('\') -replace '\\', '/'
            $artifactType = if ($artifact.Extension -eq '.msi') { 'msi' } else { 'exe' }
            $artifactRecords += [ordered]@{ path = $relative; type = $artifactType; sha256 = Get-Sha256 $artifact.FullName }
        }
        $status = if ($UnsignedEngineeringCandidate) { 'BLOCKED' } else { 'PASS' }
        $evidence = [ordered]@{
            schema_version = 1
            product = $version.product
            version = $version.version
            build_id = $version.build_id
            source_revision = $sourceRevision
            artifacts = $artifactRecords
            signing = [ordered]@{ status = $status; signed = @($artifactRecords | ForEach-Object { $_.path }) }
            timestamp = [ordered]@{ status = $status }
        }
        Write-Utf8NoBom (Join-Path $stagePath 'release-evidence.json') ($evidence | ConvertTo-Json -Depth 10)
        & (Join-Path $PSScriptRoot 'New-SpdxSbom.ps1') -InputDirectory $stagePath -OutputPath (Join-Path $stagePath 'sbom.spdx.json') -VersionJsonPath (Join-Path $script:RepoRoot 'release\version.json')
        Write-Sha256Manifest $stagePath
        return $evidence
    } finally {
        $env:CARGO_TARGET_DIR = $oldCargoTargetDir
        $env:CARGO_NET_OFFLINE = $oldCargoNetOffline
        if ($null -ne $plan) {
            Remove-OwnedBuildRoot -BuildRoot $plan.BuildRoot -StagePath $stagePath
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ReleaseBuild @PSBoundParameters
}
