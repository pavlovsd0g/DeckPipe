param(
    [string]$StagingDirectory,
    [switch]$UnsignedEngineeringCandidate,
    [switch]$PrivateBetaCandidate,
    [string]$SignToolPath,
    [string]$SigningCertificateThumbprint,
    [string]$TimestampUrl,
    [string]$PythonExe = 'python.exe',
    [string]$WheelhouseDirectory
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$script:ReleaseLabRoot = 'D:\DeckPipe-RC-Lab'

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

function Get-PythonRuntimeInfo {
    param([string]$PythonExe)
    if (-not $PythonExe) { throw 'PythonExe must point to an existing Windows x64 CPython 3.12 executable' }
    $command = Get-Command $PythonExe -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "PythonExe must point to an existing Windows x64 CPython 3.12 executable: $PythonExe" }
    $pythonPath = $command.Source
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw "PythonExe must point to an existing Windows x64 CPython 3.12 executable: $PythonExe" }

    $pythonProbe = @'
import platform, struct, sys
print(sys.executable)
print(platform.python_implementation())
print(platform.python_version())
print(platform.machine())
print(struct.calcsize('P') * 8)
'@
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $lines = @(& $pythonPath -c $pythonProbe 2>&1)
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
    if ($code -ne 0 -or $lines.Count -lt 5) {
        throw "PythonExe probe failed for ${PythonExe}: $(($lines | Out-String).Trim())"
    }
    $info = [pscustomobject]@{
        Path = [string]$lines[0]
        Implementation = [string]$lines[1]
        Version = [string]$lines[2]
        Machine = [string]$lines[3]
        Bits = [int]$lines[4]
    }
    if ($info.Implementation -ne 'CPython' -or $info.Version -notmatch '^3\.12\.' -or $info.Machine -ne 'AMD64' -or $info.Bits -ne 64) {
        throw "PythonExe must be Windows x64 CPython 3.12 AMD64/64-bit, got $($info.Implementation) $($info.Version) $($info.Machine) $($info.Bits)-bit at $($info.Path)"
    }
    return $info
}

function Get-FullPathNormalized {
    param([string]$Path)
    return [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)).TrimEnd('\')
}

function Copy-PrivateBetaPolicy {
    param([string]$DestinationDirectory)
    $policyPath = Join-Path $PSScriptRoot 'policy.json'
    if (-not (Test-Path -LiteralPath $policyPath -PathType Leaf)) { throw 'private-beta policy BLOCKED: release/policy.json is missing' }
    Copy-Item -LiteralPath $policyPath -Destination (Join-Path $DestinationDirectory 'policy.json') -Force
    return Get-Sha256 (Join-Path $DestinationDirectory 'policy.json')
}

function Assert-ReleaseBuildMode {
    param(
        [switch]$UnsignedEngineeringCandidate,
        [switch]$PrivateBetaCandidate,
        [string]$SignToolPath,
        [string]$SigningCertificateThumbprint,
        [string]$TimestampUrl
    )
    if ($PrivateBetaCandidate -and $UnsignedEngineeringCandidate) {
        throw 'PrivateBetaCandidate cannot combine with UnsignedEngineeringCandidate'
    }
    if ($PrivateBetaCandidate -and (-not [string]::IsNullOrWhiteSpace($SignToolPath) -or
            -not [string]::IsNullOrWhiteSpace($SigningCertificateThumbprint) -or
            -not [string]::IsNullOrWhiteSpace($TimestampUrl))) {
        throw 'PrivateBetaCandidate cannot combine with signing or timestamp inputs'
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
    $revisionOutput = & git -C $script:RepoRoot rev-parse HEAD 2>$null
    $revisionExitCode = $LASTEXITCODE
    $revisionItems = @($revisionOutput | Select-Object -First 1)
    $revision = if ($revisionItems.Count -gt 0) { $revisionItems[0] } else { '' }
    if (-not $revision -or $revisionExitCode -ne 0) { throw 'source provenance BLOCKED: git rev-parse HEAD failed' }
    $dirty = (& git -C $script:RepoRoot status --porcelain --untracked-files=no 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'source provenance BLOCKED: git status failed' }
    if (@($dirty).Count -gt 0) { throw 'source provenance BLOCKED: tracked source is dirty' }
    return [string]$revision
}

function Get-ExpectedArtifactNames {
    param($Version, [string]$SourceRevision, [switch]$PrivateBetaCandidate)
    $short = $SourceRevision.Substring(0, 7)
    $label = if ($PrivateBetaCandidate) { '-unsigned-private-beta' } else { '' }
    return @(
        "$($Version.artifact_name_prefix)-$short$label-x64.exe",
        "$($Version.artifact_name_prefix)-$short$label-x64-setup.exe",
        "$($Version.artifact_name_prefix)-$short$label-x64.msi"
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

function Assert-PathInside {
    param(
        [string]$Path,
        [string]$RequiredRoot,
        [string]$Message
    )
    $full = Get-FullPathNormalized $Path
    $root = [IO.Path]::GetFullPath($RequiredRoot).TrimEnd('\')
    if ($full -ine $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw $Message
    }
}

function Get-WheelhouseManifestEntries {
    param([string]$ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { throw 'dependency BLOCKED: wheelhouse-manifest.json is missing' }
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    foreach ($required in @('schema_version', 'files')) {
        if (-not ($manifest.PSObject.Properties.Name -contains $required)) { throw "dependency BLOCKED: wheelhouse manifest missing $required" }
    }
    if ([int]$manifest.schema_version -ne 1) { throw 'dependency BLOCKED: wheelhouse manifest schema_version must be 1' }
    return @($manifest.files)
}

function Assert-WheelhouseReady {
    param(
        [string]$WheelhouseDirectory,
        [string]$StagingDirectory
    )
    if (-not $WheelhouseDirectory) { throw 'dependency BLOCKED: explicit offline wheelhouse is required' }
    if (-not (Test-Path -LiteralPath $WheelhouseDirectory -PathType Container)) { throw 'dependency BLOCKED: offline wheelhouse is missing' }

    $wheelhouseFull = Get-FullPathNormalized $WheelhouseDirectory
    $repoFull = [IO.Path]::GetFullPath($script:RepoRoot).TrimEnd('\')
    Assert-PathNotInside -Path $wheelhouseFull -ForbiddenRoot $repoFull -Message 'dependency BLOCKED: wheelhouse cannot be inside the repository'
    if ($StagingDirectory) {
        $stageFull = Get-FullPathNormalized $StagingDirectory
        Assert-PathNotInside -Path $wheelhouseFull -ForbiddenRoot $stageFull -Message 'dependency BLOCKED: wheelhouse cannot be inside staging'
    }
    Assert-PathInside -Path $wheelhouseFull -RequiredRoot $script:ReleaseLabRoot -Message 'dependency BLOCKED: wheelhouse must live below D:\DeckPipe-RC-Lab'

    $wheelhouseItem = Get-Item -LiteralPath $wheelhouseFull -Force
    if (($wheelhouseItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'dependency BLOCKED: wheelhouse reparse point is not allowed' }

    foreach ($entry in @(Get-ChildItem -LiteralPath $wheelhouseFull -Force | Where-Object { $_.PSIsContainer })) {
        throw "dependency BLOCKED: wheelhouse subdirectories are not allowed: $($entry.Name)"
    }

    $manifestPath = Join-Path $wheelhouseFull 'wheelhouse-manifest.json'
    $manifestEntries = Get-WheelhouseManifestEntries -ManifestPath $manifestPath
    if (@($manifestEntries).Count -eq 0) { throw 'dependency BLOCKED: wheelhouse manifest is empty' }

    $actualByName = @{}
    foreach ($file in @(Get-ChildItem -LiteralPath $wheelhouseFull -Force -File | Where-Object { $_.Name -ne 'wheelhouse-manifest.json' } | Sort-Object Name)) {
        if ($file.Name -notmatch '(?i)\.whl$') { throw "dependency BLOCKED: wheelhouse contains non-wheel file: $($file.Name)" }
        $key = $file.Name.ToLowerInvariant()
        if ($actualByName.ContainsKey($key)) { throw "dependency BLOCKED: wheelhouse duplicate or case-confusable file: $($file.Name)" }
        $actualByName[$key] = [pscustomobject]@{
            Name = $file.Name
            Size = [int64]$file.Length
            Sha256 = Get-Sha256 $file.FullName
        }
    }

    $manifestByName = @{}
    foreach ($entry in @($manifestEntries)) {
        foreach ($required in @('name', 'size', 'sha256')) {
            if (-not ($entry.PSObject.Properties.Name -contains $required)) { throw "dependency BLOCKED: wheelhouse manifest entry missing $required" }
        }
        $name = [string]$entry.name
        if ($name -match '^[A-Za-z]:|^/|\\|/|:|\.\.') { throw "dependency BLOCKED: unsafe wheelhouse manifest name: $name" }
        if ($name -notmatch '(?i)\.whl$') { throw "dependency BLOCKED: wheelhouse manifest contains non-wheel file: $name" }
        if ([string]$entry.sha256 -cnotmatch '^[0-9a-f]{64}$') { throw "dependency BLOCKED: wheelhouse manifest hash is not lowercase SHA-256: $name" }
        $key = $name.ToLowerInvariant()
        if ($manifestByName.ContainsKey($key)) { throw "dependency BLOCKED: wheelhouse manifest duplicate or case-confusable name: $name" }
        $manifestByName[$key] = $entry
    }

    foreach ($key in @($manifestByName.Keys)) {
        if (-not $actualByName.ContainsKey($key)) { throw "dependency BLOCKED: wheelhouse manifest/file mismatch missing: $($manifestByName[$key].name)" }
    }
    foreach ($key in @($actualByName.Keys)) {
        if (-not $manifestByName.ContainsKey($key)) { throw "dependency BLOCKED: wheelhouse manifest/file mismatch extra: $($actualByName[$key].Name)" }
    }
    foreach ($key in @($manifestByName.Keys)) {
        $actual = $actualByName[$key]
        $entry = $manifestByName[$key]
        if ([int64]$entry.size -ne $actual.Size) { throw "dependency BLOCKED: wheelhouse manifest size mismatch: $($actual.Name)" }
        if ([string]$entry.sha256 -cne $actual.Sha256) { throw "dependency BLOCKED: wheelhouse hash mismatch: $($actual.Name)" }
    }

    return [pscustomobject]@{
        Path = $manifestPath
        Sha256 = Get-Sha256 $manifestPath
        Files = @($actualByName.Values | Sort-Object Name)
    }
}

function New-ReleaseBuildPlan {
    param(
        [string]$StagingDirectory,
        [string]$WheelhouseDirectory,
        $Version,
        [string]$SourceRevision,
        [string]$PythonExe = 'python.exe',
        [switch]$PrivateBetaCandidate
    )
    if (-not $WheelhouseDirectory) { throw 'dependency BLOCKED: explicit offline wheelhouse is required' }
    if (-not $SourceRevision -or $SourceRevision -notmatch '^[0-9a-f]{40}$') { throw 'source provenance BLOCKED: exact 40-character tracked HEAD is required' }

    $stagePath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($StagingDirectory)
    $repoFull = [IO.Path]::GetFullPath($script:RepoRoot)
    $stageFull = [IO.Path]::GetFullPath($stagePath)
    $manifestRecord = Assert-WheelhouseReady -WheelhouseDirectory $WheelhouseDirectory -StagingDirectory $stageFull
    $pythonInfo = Get-PythonRuntimeInfo -PythonExe $PythonExe
    $buildBase = Join-Path $script:ReleaseLabRoot 'build'
    $buildRoot = Join-Path $buildBase ("deckpipe-release-build-" + [guid]::NewGuid().ToString('N'))
    Assert-PathNotInside -Path $buildRoot -ForbiddenRoot $repoFull -Message 'build workspace must be outside the repository'
    Assert-PathNotInside -Path $buildRoot -ForbiddenRoot $stageFull -Message 'build workspace must be outside staging'
    Assert-PathInside -Path $buildRoot -RequiredRoot $buildBase -Message 'build workspace must be below D:\DeckPipe-RC-Lab\build'

    $sourceRoot = Join-Path $buildRoot 'source'
    $venvPath = Join-Path $buildRoot 'venv'
    $venvPython = Join-Path $venvPath 'Scripts\python.exe'
    $distPath = Join-Path $buildRoot 'pyinstaller-dist'
    $workPath = Join-Path $buildRoot 'pyinstaller-work'
    $specPath = Join-Path $buildRoot 'pyinstaller-spec'
    $staticAssetSource = Join-Path $sourceRoot 'app\static'
    $cargoTarget = Join-Path $buildRoot 'cargo-target'
    $bundleRoot = Join-Path $cargoTarget 'release\bundle'
    $wheelhouseFull = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($WheelhouseDirectory)

    $pipInstallCommands = @(
        [pscustomobject]@{ Arguments = @('-m', 'pip', 'install', '--no-cache-dir', '--no-index', '--require-hashes', '--find-links', $wheelhouseFull, '-r', (Join-Path $sourceRoot 'requirements-build.lock')) },
        [pscustomobject]@{ Arguments = @('-m', 'pip', 'install', '--no-cache-dir', '--no-index', '--require-hashes', '--find-links', $wheelhouseFull, '-r', (Join-Path $sourceRoot 'requirements.lock')) }
    )
    $pyInstallerArguments = @(
        '-m', 'PyInstaller', '--noconfirm', '--onefile', '--name', 'deckpipe-backend',
        '--distpath', $distPath, '--workpath', $workPath, '--specpath', $specPath,
        '--collect-all', 'yt_dlp', '--collect-all', 'deezer_python_gql', '--collect-all', 'imageio_ffmpeg',
        '--collect-all', 'uvicorn', '--collect-all', 'sqlcipher3',
        '--add-data', "$staticAssetSource;app/static",
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
        WheelhouseManifestPath = $manifestRecord.Path
        WheelhouseManifestSha256 = $manifestRecord.Sha256
        CargoTargetDir = $cargoTarget
        BundleRoot = $bundleRoot
        ExpectedArtifactNames = @(Get-ExpectedArtifactNames -Version $Version -SourceRevision $SourceRevision -PrivateBetaCandidate:$PrivateBetaCandidate)
        ApplicationArtifactName = @(Get-ExpectedArtifactNames -Version $Version -SourceRevision $SourceRevision -PrivateBetaCandidate:$PrivateBetaCandidate)[0]
        SetupArtifactName = @(Get-ExpectedArtifactNames -Version $Version -SourceRevision $SourceRevision -PrivateBetaCandidate:$PrivateBetaCandidate)[1]
        MsiArtifactName = @(Get-ExpectedArtifactNames -Version $Version -SourceRevision $SourceRevision -PrivateBetaCandidate:$PrivateBetaCandidate)[2]
        PythonExe = $pythonInfo.Path
        PythonRuntime = $pythonInfo
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

function New-OwnedCandidateDirectory {
    param([string]$StagePath)
    $stageFull = [IO.Path]::GetFullPath($StagePath).TrimEnd('\')
    $stageParent = Split-Path -Parent $stageFull
    $stageLeaf = Split-Path -Leaf $stageFull
    if (-not $stageParent -or -not (Test-Path -LiteralPath $stageParent -PathType Container)) { throw 'staging directory parent is missing' }
    $candidate = Join-Path $stageParent (".$stageLeaf.candidate-" + [guid]::NewGuid().ToString('N'))
    Assert-PathNotInside -Path $candidate -ForbiddenRoot $script:RepoRoot -Message 'candidate output directory must be outside the repository'
    Assert-PathNotInside -Path $candidate -ForbiddenRoot $stageFull -Message 'candidate output directory must be outside final staging'
    return $candidate
}

function Remove-OwnedCandidateDirectory {
    param([string]$CandidateDirectory, [string]$StagePath)
    if (-not $CandidateDirectory -or -not (Test-Path -LiteralPath $CandidateDirectory)) { return }
    Assert-PathNotInside -Path $CandidateDirectory -ForbiddenRoot $script:RepoRoot -Message 'refusing to remove a repository path as release candidate'
    Assert-PathNotInside -Path $CandidateDirectory -ForbiddenRoot $StagePath -Message 'refusing to remove the final staging path as release candidate'
    Remove-Item -LiteralPath $CandidateDirectory -Recurse -Force
}

function Invoke-ReleaseVerifierForCandidate {
    param(
        [string]$CandidateDirectory,
        [switch]$UnsignedEngineeringCandidate,
        [switch]$PrivateBetaCandidate
    )
    $verifyScript = Join-Path $PSScriptRoot 'verify.ps1'
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $verifyScript -StagingDirectory $CandidateDirectory 2>&1
    $exitCode = $LASTEXITCODE
    $text = ($output | Out-String)
    try {
        $result = $text | ConvertFrom-Json
    } catch {
        throw "candidate verification failed: verifier did not return clean JSON: $text"
    }
    if ($UnsignedEngineeringCandidate) {
        if ($exitCode -ne 2 -or $result.status -ne 'BLOCKED') {
            throw "candidate verification failed: unsigned engineering candidate must verify as BLOCKED, got exit $exitCode status $($result.status): $($result.message)"
        }
        if ($result.message -notmatch 'signing|timestamp') {
            throw "candidate verification failed: unsigned engineering candidate is not blocked on signing/timestamp: $($result.message)"
        }
        return $result
    }
    if ($PrivateBetaCandidate) {
        if ($exitCode -ne 0 -or $result.status -ne 'PASS') {
            throw "candidate verification failed: private beta candidate must verify as PASS, got exit $exitCode status $($result.status): $($result.message)"
        }
        if ($result.message -notmatch 'private-beta|policy|waiver') {
            throw "candidate verification failed: private beta candidate did not bind policy waiver: $($result.message)"
        }
        return $result
    }
    if ($exitCode -ne 0 -or $result.status -ne 'PASS') {
        throw "candidate verification failed: signed release candidate must verify as PASS, got exit $exitCode status $($result.status): $($result.message)"
    }
    return $result
}

function Invoke-ReleaseCandidateTransaction {
    param(
        [string]$StagingDirectory,
        $Version,
        [string]$SourceRevision,
        [switch]$UnsignedEngineeringCandidate,
        [switch]$PrivateBetaCandidate,
        [scriptblock]$AssembleCandidate,
        [scriptblock]$ValidateCandidate
    )
    if ($null -eq $AssembleCandidate) { throw 'candidate transaction requires an assembly step' }
    if ($null -eq $ValidateCandidate) { throw 'candidate transaction requires a validation step' }

    $stagePath = Assert-StagingDirectorySafe $StagingDirectory
    $candidatePath = New-OwnedCandidateDirectory -StagePath $stagePath
    $expectedNames = @(Get-ExpectedArtifactNames -Version $Version -SourceRevision $SourceRevision -PrivateBetaCandidate:$PrivateBetaCandidate)
    try {
        [IO.Directory]::CreateDirectory($candidatePath) | Out-Null
        & $AssembleCandidate $candidatePath $expectedNames | ForEach-Object { Write-Host $_ }
        & $ValidateCandidate $candidatePath | ForEach-Object { Write-Host $_ }

        if (Test-Path -LiteralPath $stagePath) {
            $entries = @(Get-ChildItem -LiteralPath $stagePath -Force)
            if ($entries.Count -gt 0) { throw 'staging directory became nonempty before publish' }
            [IO.Directory]::Delete($stagePath, $false)
        }
        [IO.Directory]::Move($candidatePath, $stagePath)
        $candidatePath = $null
        return $stagePath
    } finally {
        if ($candidatePath) {
            Remove-OwnedCandidateDirectory -CandidateDirectory $candidatePath -StagePath $stagePath
        }
    }
}

function Invoke-ReleaseBuild {
    param(
        [string]$StagingDirectory,
        [switch]$UnsignedEngineeringCandidate,
        [switch]$PrivateBetaCandidate,
        [string]$SignToolPath,
        [string]$SigningCertificateThumbprint,
        [string]$TimestampUrl,
        [string]$PythonExe = 'python.exe',
        [string]$WheelhouseDirectory
    )

    $plan = $null
    Assert-ReleaseBuildMode -UnsignedEngineeringCandidate:$UnsignedEngineeringCandidate -PrivateBetaCandidate:$PrivateBetaCandidate -SignToolPath $SignToolPath -SigningCertificateThumbprint $SigningCertificateThumbprint -TimestampUrl $TimestampUrl
    $version = Assert-VersionSync
    Assert-PythonLocksReady
    $sourceRevision = Get-SourceRevision
    $stagePath = Assert-StagingDirectorySafe $StagingDirectory

    if (-not $UnsignedEngineeringCandidate -and -not $PrivateBetaCandidate) {
        if (-not $SignToolPath) { throw 'signing BLOCKED: SignToolPath is required' }
        if (-not $SigningCertificateThumbprint) { throw 'signing BLOCKED: exact certificate thumbprint is required' }
        if (-not $TimestampUrl) { throw 'timestamp BLOCKED: RFC3161 TimestampUrl is required' }
    }

    $plan = New-ReleaseBuildPlan -StagingDirectory $stagePath -WheelhouseDirectory $WheelhouseDirectory -Version $version -SourceRevision $sourceRevision -PythonExe $PythonExe -PrivateBetaCandidate:$PrivateBetaCandidate
    [IO.Directory]::CreateDirectory($plan.BuildRoot) | Out-Null
    $oldCargoTargetDir = $env:CARGO_TARGET_DIR
    $oldCargoNetOffline = $env:CARGO_NET_OFFLINE
    $oldCargoHome = $env:CARGO_HOME
    $oldRustupHome = $env:RUSTUP_HOME
    $oldPipNoCacheDir = $env:PIP_NO_CACHE_DIR
    try {
        $env:PIP_NO_CACHE_DIR = '1'
        $publishedStage = Invoke-ReleaseCandidateTransaction -StagingDirectory $stagePath -Version $version -SourceRevision $sourceRevision -UnsignedEngineeringCandidate:$UnsignedEngineeringCandidate -PrivateBetaCandidate:$PrivateBetaCandidate -AssembleCandidate {
            param($candidatePath, $expectedNames)

            Export-TrackedSourceToTemp -Plan $plan
            & $plan.PythonExe -m venv $plan.VenvPath
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
                & npm run build -- --verbose
                if ($LASTEXITCODE -ne 0) { throw 'build failed: Tauri build failed' }
            } finally {
                Pop-Location
            }

            $applicationExe = Join-Path $plan.CargoTargetDir 'release\deckpipe.exe'
            if (-not (Test-Path -LiteralPath $applicationExe -PathType Leaf)) { throw 'build failed: application exe was not produced in temp cargo target' }
            Copy-Item -LiteralPath $applicationExe -Destination (Join-Path $candidatePath $plan.ApplicationArtifactName)

            $builtArtifacts = Get-ReleaseArtifacts $plan.BundleRoot
            $setupArtifacts = @($builtArtifacts | Where-Object { $_.Name -match '(?i)setup\.exe$' })
            $msiArtifacts = @($builtArtifacts | Where-Object { $_.Extension -ieq '.msi' })
            if ($setupArtifacts.Count -ne 1) { throw 'build failed: expected exactly one setup executable artifact' }
            if ($msiArtifacts.Count -ne 1) { throw 'build failed: expected exactly one MSI artifact' }
            Copy-Item -LiteralPath $setupArtifacts[0].FullName -Destination (Join-Path $candidatePath $plan.SetupArtifactName)
            Copy-Item -LiteralPath $msiArtifacts[0].FullName -Destination (Join-Path $candidatePath $plan.MsiArtifactName)

            Remove-OwnedBuildRoot -BuildRoot $plan.BuildRoot -StagePath $stagePath

            $candidateArtifacts = Get-TopLevelReleaseArtifacts $candidatePath
            $expectedByName = @{}
            foreach ($name in @($expectedNames)) { $expectedByName[$name.ToLowerInvariant()] = $name }
            foreach ($artifact in $candidateArtifacts) {
                if (-not $expectedByName.ContainsKey($artifact.Name.ToLowerInvariant())) { throw "unexpected candidate artifact: $($artifact.Name)" }
                $expectedByName.Remove($artifact.Name.ToLowerInvariant())
            }
            if ($expectedByName.Count -gt 0) { throw "build failed: missing expected artifact(s): $((@($expectedByName.Values) | Sort-Object) -join ', ')" }

            if (-not $UnsignedEngineeringCandidate -and -not $PrivateBetaCandidate) {
                Invoke-ArtifactSigning -SignToolPath $SignToolPath -Artifacts @($candidateArtifacts | ForEach-Object { $_.FullName }) -CertificateThumbprint $SigningCertificateThumbprint -TimestampUrl $TimestampUrl
            }

            $artifactRecords = @()
            foreach ($artifact in $candidateArtifacts) {
                $relative = $artifact.FullName.Substring($candidatePath.Length).TrimStart('\') -replace '\\', '/'
                $artifactType = if ($artifact.Extension -eq '.msi') { 'msi' } else { 'exe' }
                $artifactRecords += [ordered]@{ path = $relative; type = $artifactType; sha256 = Get-Sha256 $artifact.FullName }
            }
            $policySha256 = ''
            if ($PrivateBetaCandidate) {
                $policySha256 = Copy-PrivateBetaPolicy -DestinationDirectory $candidatePath
            }
            $status = if ($UnsignedEngineeringCandidate) { 'BLOCKED' } elseif ($PrivateBetaCandidate) { 'WAIVED_BY_OWNER' } else { 'PASS' }
            $signedArtifacts = @($artifactRecords | ForEach-Object { $_.path })
            if ($PrivateBetaCandidate) { $signedArtifacts = @() }
            $evidence = [ordered]@{
                schema_version = 1
                product = $version.product
                version = $version.version
                build_id = $version.build_id
                source_revision = $sourceRevision
                dependencies = [ordered]@{
                    wheelhouse_manifest_path = 'wheelhouse-manifest.json'
                    wheelhouse_manifest_sha256 = $plan.WheelhouseManifestSha256
                }
                artifacts = $artifactRecords
                signing = [ordered]@{ status = $status; signed = $signedArtifacts }
                timestamp = [ordered]@{ status = $status }
            }
            if ($PrivateBetaCandidate) {
                $evidence['distribution'] = [ordered]@{
                    channel = 'private-beta'
                    artifact_label = 'unsigned-private-beta'
                    policy_path = 'policy.json'
                    policy_sha256 = $policySha256
                }
                $evidence.signing['policy_path'] = 'policy.json'
                $evidence.timestamp['policy_path'] = 'policy.json'
            }
            Write-Utf8NoBom (Join-Path $candidatePath 'release-evidence.json') ($evidence | ConvertTo-Json -Depth 10)
            & (Join-Path $PSScriptRoot 'New-SpdxSbom.ps1') -InputDirectory $candidatePath -OutputPath (Join-Path $candidatePath 'sbom.spdx.json') -VersionJsonPath (Join-Path $script:RepoRoot 'release\version.json')
            Write-Sha256Manifest $candidatePath
        } -ValidateCandidate {
            param($candidatePath)
            Invoke-ReleaseVerifierForCandidate -CandidateDirectory $candidatePath -UnsignedEngineeringCandidate:$UnsignedEngineeringCandidate -PrivateBetaCandidate:$PrivateBetaCandidate | Out-Null
        }
        $evidence = Get-Content -LiteralPath (Join-Path $publishedStage 'release-evidence.json') -Raw | ConvertFrom-Json
        return $evidence
    } finally {
        $env:CARGO_TARGET_DIR = $oldCargoTargetDir
        $env:CARGO_NET_OFFLINE = $oldCargoNetOffline
        $env:CARGO_HOME = $oldCargoHome
        $env:RUSTUP_HOME = $oldRustupHome
        $env:PIP_NO_CACHE_DIR = $oldPipNoCacheDir
        if ($null -ne $plan) {
            Remove-OwnedBuildRoot -BuildRoot $plan.BuildRoot -StagePath $stagePath
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ReleaseBuild @PSBoundParameters
}
