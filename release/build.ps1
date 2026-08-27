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
        "$($Version.artifact_name_prefix)-$short-x64-setup.exe",
        "$($Version.artifact_name_prefix)-$short-x64.msi"
    )
}

function Get-ReleaseArtifacts {
    param([string]$Directory)
    if (-not (Test-Path -LiteralPath $Directory)) { return @() }
    return @(Get-ChildItem -LiteralPath $Directory -Recurse -Force -File |
        Where-Object { $_.Extension -in @('.exe', '.msi') } |
        Sort-Object FullName)
}

function Write-Sha256Manifest {
    param([string]$Directory)
    $lines = @()
    $seen = @{}
    $files = @(Get-ChildItem -LiteralPath $Directory -Recurse -Force -File |
        Where-Object { $_.Name -ne 'SHA256SUMS.txt' } |
        Sort-Object FullName)
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

    $stagePath = Assert-StagingDirectorySafe $StagingDirectory
    $version = Assert-VersionSync
    Assert-PythonLocksReady
    $sourceRevision = Get-SourceRevision

    if (-not $UnsignedEngineeringCandidate) {
        if (-not $SignToolPath) { throw 'signing BLOCKED: SignToolPath is required' }
        if (-not $SigningCertificateThumbprint) { throw 'signing BLOCKED: exact certificate thumbprint is required' }
        if (-not $TimestampUrl) { throw 'timestamp BLOCKED: RFC3161 TimestampUrl is required' }
    }
    if ($WheelhouseDirectory -and -not (Test-Path -LiteralPath $WheelhouseDirectory)) {
        throw 'dependency BLOCKED: offline wheelhouse is missing'
    }

    [IO.Directory]::CreateDirectory($stagePath) | Out-Null
    $ownedBuildRoot = Join-Path $stagePath ("_deckpipe-build-" + [guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($ownedBuildRoot) | Out-Null
    try {
        $venvPath = Join-Path $ownedBuildRoot 'venv'
        & $PythonExe -m venv $venvPath
        if ($LASTEXITCODE -ne 0) { throw 'build failed: Python venv creation failed' }
        $venvPython = Join-Path $venvPath 'Scripts\python.exe'
        $pipArgs = @('-m', 'pip', 'install', '--no-index', '--require-hashes', '-r', (Join-Path $script:RepoRoot 'requirements-build.lock'))
        if ($WheelhouseDirectory) { $pipArgs += @('--find-links', $WheelhouseDirectory) }
        & $venvPython @pipArgs
        if ($LASTEXITCODE -ne 0) { throw 'build failed: offline Python build dependency install failed' }

        & $venvPython -m PyInstaller --noconfirm --onefile --name deckpipe-backend `
            --collect-all yt_dlp --collect-all deezer_python_gql --collect-all imageio_ffmpeg `
            --collect-all uvicorn --collect-all sqlcipher3 `
            --add-data "app/static;app/static" `
            --hidden-import pyrekordbox --hidden-import mutagen --hidden-import Crypto `
            --hidden-import app.main --hidden-import app.jobs --hidden-import app.library `
            --hidden-import app.deezer_client --hidden-import app.soundcloud --hidden-import app.tagger `
            --hidden-import app.converter --hidden-import app.bugreport --hidden-import app.rekordbox `
            --hidden-import uvicorn.loops.auto --hidden-import uvicorn.protocols.http.auto `
            --hidden-import uvicorn.protocols.websockets.auto --hidden-import uvicorn.lifespan.on `
            (Join-Path $script:RepoRoot 'run_backend.py')
        if ($LASTEXITCODE -ne 0) { throw 'build failed: PyInstaller backend build failed' }

        $sidecarDir = Join-Path $script:RepoRoot 'desktop\src-tauri\binaries'
        [IO.Directory]::CreateDirectory($sidecarDir) | Out-Null
        Copy-Item -LiteralPath (Join-Path $script:RepoRoot 'dist\deckpipe-backend.exe') -Destination (Join-Path $sidecarDir 'deckpipe-backend-x86_64-pc-windows-msvc.exe') -Force
        Push-Location (Join-Path $script:RepoRoot 'desktop')
        try {
            & npm ci --offline
            if ($LASTEXITCODE -ne 0) { throw 'build failed: npm ci --offline failed' }
            & npm run build
            if ($LASTEXITCODE -ne 0) { throw 'build failed: Tauri build failed' }
        } finally {
            Pop-Location
        }

        $bundleRoot = if ($env:CARGO_TARGET_DIR) { Join-Path $env:CARGO_TARGET_DIR 'release\bundle' } else { Join-Path $script:RepoRoot 'desktop\src-tauri\target\release\bundle' }
        $builtArtifacts = Get-ReleaseArtifacts $bundleRoot
        if ($builtArtifacts.Count -eq 0) { throw 'build failed: no expected exe/msi artifacts were produced' }

        foreach ($artifact in $builtArtifacts) {
            $newName = "$($version.artifact_name_prefix)-$($sourceRevision.Substring(0, 7))-$($artifact.Name)"
            Copy-Item -LiteralPath $artifact.FullName -Destination (Join-Path $stagePath $newName)
        }
        $stagedArtifacts = Get-ReleaseArtifacts $stagePath
        if ($stagedArtifacts.Count -eq 0) { throw 'build failed: no expected exe/msi artifacts were staged' }

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
        if (Test-Path -LiteralPath $ownedBuildRoot) {
            Remove-Item -LiteralPath $ownedBuildRoot -Recurse -Force
        }
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    Invoke-ReleaseBuild @PSBoundParameters
}
