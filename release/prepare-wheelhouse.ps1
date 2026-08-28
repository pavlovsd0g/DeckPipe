param(
    [Parameter(Mandatory)][string]$LabRoot,
    [Parameter(Mandatory)][string]$PythonExe,
    [Parameter(Mandatory)][string]$PipToolsVersion
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:RepoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$script:ExpectedLabRoot = 'D:\DeckPipe-RC-Lab'
$script:LabDirectories = @(
    'downloads',
    'wheelhouse',
    'tool-cache',
    'build',
    'staging',
    'install',
    'backups\rekordbox',
    'qa-evidence',
    'logs-redacted'
)

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

function Get-FullPathNormalized {
    param([string]$Path)
    return [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)).TrimEnd('\')
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

function Assert-LabRoot {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Path)).TrimEnd('\')
    $expected = [IO.Path]::GetFullPath($script:ExpectedLabRoot).TrimEnd('\')
    if ($full -ine $expected) {
        throw "LabRoot must be exactly $script:ExpectedLabRoot"
    }
    return $full
}

function Clear-OwnedDirectory {
    param([string]$Path)
    $full = Get-FullPathNormalized $Path
    Assert-PathInside -Path $full -RequiredRoot $script:ExpectedLabRoot -Message "refusing to clean path outside $script:ExpectedLabRoot"
    if (Test-Path -LiteralPath $full) {
        $item = Get-Item -LiteralPath $full -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "refusing to clean reparse point: $full" }
        foreach ($child in @(Get-ChildItem -LiteralPath $full -Force)) {
            Remove-Item -LiteralPath $child.FullName -Recurse -Force
        }
    } else {
        [IO.Directory]::CreateDirectory($full) | Out-Null
    }
}

function Invoke-Checked {
    param(
        [string]$Exe,
        [string[]]$Arguments,
        [string]$Step
    )
    $oldErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $oldErrorActionPreference
    }
    $text = ($output | Out-String).Trim()
    if ($code -ne 0) {
        throw "$Step failed with exit $code. $text"
    }
    return $text
}

function Get-CommandRecord {
    param(
        [string]$Name,
        [string[]]$VersionArguments = @('--version')
    )
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) {
        return [ordered]@{ available = $false; path = ''; version = '' }
    }
    $version = ''
    try {
        $versionOutput = & $command.Source @VersionArguments 2>&1
        $version = (($versionOutput | Select-Object -First 3) -join "`n").Trim()
    } catch {
        $version = "version probe failed: $($_.Exception.Message)"
    }
    return [ordered]@{ available = $true; path = $command.Source; version = $version }
}

function Get-OptionalRegistryDisplay {
    param([string]$Pattern)
    $matches = @()
    foreach ($root in @(
            'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
            'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
        )) {
        try {
            $matches += @(Get-ItemProperty -Path $root -ErrorAction SilentlyContinue |
                Where-Object { $_.DisplayName -match $Pattern } |
                Select-Object -First 5 DisplayName, DisplayVersion, Publisher)
        } catch {
        }
    }
    return @($matches)
}

function Write-HostToolInventory {
    param(
        [string]$LabRoot,
        [string]$PythonExe,
        [string]$InventoryPath
    )
    $pythonProbe = @'
import platform, struct, sys
print(sys.executable)
print(platform.python_implementation())
print(platform.python_version())
print(platform.machine())
print(struct.calcsize('P') * 8)
'@
    $pythonProbeLines = @(& $PythonExe -c $pythonProbe)
    if ($LASTEXITCODE -ne 0 -or $pythonProbeLines.Count -lt 5) {
        throw 'PythonExe probe failed'
    }
    $pythonInfo = [pscustomobject][ordered]@{
        executable = [string]$pythonProbeLines[0]
        implementation = [string]$pythonProbeLines[1]
        version = [string]$pythonProbeLines[2]
        machine = [string]$pythonProbeLines[3]
        bits = [int]$pythonProbeLines[4]
    }
    if ($pythonInfo.implementation -ne 'CPython' -or $pythonInfo.version -notmatch '^3\.12\.' -or [int]$pythonInfo.bits -ne 64) {
        throw "PythonExe must be Windows x64 CPython 3.12, got $($pythonInfo.implementation) $($pythonInfo.version) $($pythonInfo.bits)-bit at $($pythonInfo.executable)"
    }

    $os = Get-CimInstance Win32_OperatingSystem
    $dDrive = Get-PSDrive -Name D
    $inventory = [ordered]@{
        schema_version = 1
        generated_at_utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
        lab_root = $LabRoot
        windows = [ordered]@{
            caption = $os.Caption
            version = $os.Version
            build_number = $os.BuildNumber
            os_architecture = $os.OSArchitecture
        }
        d_drive = [ordered]@{
            free_bytes = [int64]$dDrive.Free
            used_bytes = [int64]$dDrive.Used
        }
        python = $pythonInfo
        node = Get-CommandRecord 'node.exe'
        npm = Get-CommandRecord 'npm.cmd'
        rustc = Get-CommandRecord 'rustc.exe' @('--version')
        cargo = Get-CommandRecord 'cargo.exe' @('--version')
        powershell = [ordered]@{
            executable = (Get-Command powershell.exe).Source
            version = $PSVersionTable.PSVersion.ToString()
        }
        webview2 = @(
            Get-OptionalRegistryDisplay 'WebView2'
        )
        rekordbox = [ordered]@{
            command = Get-CommandRecord 'rekordbox.exe' @('--version')
            installed_programs = @(Get-OptionalRegistryDisplay 'rekordbox')
            note = 'Program/tool inventory only; no library, account, browser, or content data inspected.'
        }
    }
    Write-Utf8NoBom $InventoryPath ($inventory | ConvertTo-Json -Depth 10)
}

function Initialize-LabRoot {
    param([string]$LabRoot)
    [IO.Directory]::CreateDirectory($LabRoot) | Out-Null
    foreach ($relative in $script:LabDirectories) {
        [IO.Directory]::CreateDirectory((Join-Path $LabRoot $relative)) | Out-Null
    }
}

function Write-WheelhouseManifest {
    param(
        [string]$WheelhouseDirectory,
        [string]$OutputPath,
        $PythonInfo,
        [string]$PipToolsVersion
    )
    $files = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $WheelhouseDirectory -Force -File | Sort-Object Name)) {
        if ($file.Name -notmatch '(?i)\.whl$') {
            throw "wheelhouse contains non-wheel file: $($file.Name)"
        }
        $files += [ordered]@{
            name = $file.Name
            size = [int64]$file.Length
            sha256 = Get-Sha256 $file.FullName
        }
    }
    if ($files.Count -eq 0) { throw 'wheelhouse is empty after download' }
    $manifest = [ordered]@{
        schema_version = 1
        generator = [ordered]@{
            script = 'release/prepare-wheelhouse.ps1'
            pip_tools = $PipToolsVersion
        }
        target = [ordered]@{
            implementation = $PythonInfo.implementation
            python_version = $PythonInfo.version
            machine = $PythonInfo.machine
            bits = [int]$PythonInfo.bits
            platform = 'win_amd64'
        }
        files = $files
    }
    Write-Utf8NoBom $OutputPath ($manifest | ConvertTo-Json -Depth 8)
}

function Add-RequireHashesOption {
    param([string]$LockPath)
    $text = Get-Content -LiteralPath $LockPath -Raw
    if ($text -notmatch '(?m)^--require-hashes$') {
        Write-Utf8NoBom $LockPath ("--require-hashes`n" + $text)
    }
}

$lab = Assert-LabRoot $LabRoot
Initialize-LabRoot $lab

$pythonFull = (Resolve-Path -LiteralPath $PythonExe).Path
$inventoryPath = Join-Path $lab 'qa-evidence\host-tool-inventory.json'
Write-HostToolInventory -LabRoot $lab -PythonExe $pythonFull -InventoryPath $inventoryPath
$pythonInfo = (Get-Content -LiteralPath $inventoryPath -Raw | ConvertFrom-Json).python

$downloads = Join-Path $lab 'downloads'
$wheelhouse = Join-Path $lab 'wheelhouse'
$toolCache = Join-Path $lab 'tool-cache'
$pipCache = Join-Path $toolCache 'pip-cache'
$toolVenv = Join-Path $toolCache "pip-tools-$PipToolsVersion"
$offlineVenv = Join-Path $lab 'install\offline-proof-prepare'
$offlineProofPath = Join-Path $lab 'qa-evidence\offline-install-prepare.json'
$manifestPath = Join-Path $wheelhouse 'wheelhouse-manifest.json'

Clear-OwnedDirectory $downloads
Clear-OwnedDirectory $wheelhouse
Clear-OwnedDirectory $pipCache
Clear-OwnedDirectory $offlineVenv
[IO.Directory]::CreateDirectory($toolCache) | Out-Null

$oldPipCacheDir = $env:PIP_CACHE_DIR
$oldPipDisableVersionCheck = $env:PIP_DISABLE_PIP_VERSION_CHECK
try {
    $env:PIP_CACHE_DIR = $pipCache
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'

    if (-not (Test-Path -LiteralPath (Join-Path $toolVenv 'Scripts\python.exe') -PathType Leaf)) {
        Invoke-Checked $pythonFull @('-m', 'venv', $toolVenv) 'create pip-tools venv' | Out-Null
    }
    $toolPython = Join-Path $toolVenv 'Scripts\python.exe'
    Invoke-Checked $toolPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--cache-dir', $pipCache, "pip-tools==$PipToolsVersion") 'install pip-tools' | Out-Null

    Invoke-Checked $toolPython @('-m', 'piptools', 'compile', '--generate-hashes', '--allow-unsafe', '--strip-extras', '--output-file', (Join-Path $script:RepoRoot 'requirements.lock'), (Join-Path $script:RepoRoot 'requirements.in')) 'compile runtime lock' | Out-Null
    Invoke-Checked $toolPython @('-m', 'piptools', 'compile', '--generate-hashes', '--allow-unsafe', '--strip-extras', '--output-file', (Join-Path $script:RepoRoot 'requirements-build.lock'), (Join-Path $script:RepoRoot 'requirements-build.in')) 'compile build lock' | Out-Null
    Add-RequireHashesOption (Join-Path $script:RepoRoot 'requirements.lock')
    Add-RequireHashesOption (Join-Path $script:RepoRoot 'requirements-build.lock')

    Invoke-Checked $toolPython @(
        '-m', 'pip', 'download',
        '--disable-pip-version-check',
        '--cache-dir', $pipCache,
        '--dest', $wheelhouse,
        '--require-hashes',
        '--only-binary=:all:',
        '-r', (Join-Path $script:RepoRoot 'requirements-build.lock'),
        '-r', (Join-Path $script:RepoRoot 'requirements.lock')
    ) 'download wheelhouse' | Out-Null

    foreach ($file in @(Get-ChildItem -LiteralPath $wheelhouse -Force -File)) {
        if ($file.Extension -ine '.whl') {
            throw "downloaded non-wheel artifact: $($file.Name)"
        }
    }
    Write-WheelhouseManifest -WheelhouseDirectory $wheelhouse -OutputPath $manifestPath -PythonInfo $pythonInfo -PipToolsVersion $PipToolsVersion

    Invoke-Checked $pythonFull @('-m', 'venv', $offlineVenv) 'create offline proof venv' | Out-Null
    $offlinePython = Join-Path $offlineVenv 'Scripts\python.exe'
    $offlineOutput = Invoke-Checked $offlinePython @(
        '-m', 'pip', 'install',
        '--disable-pip-version-check',
        '--no-index',
        '--find-links', $wheelhouse,
        '--require-hashes',
        '-r', (Join-Path $script:RepoRoot 'requirements-build.lock'),
        '-r', (Join-Path $script:RepoRoot 'requirements.lock')
    ) 'offline proof install'
    $pipCheckOutput = Invoke-Checked $offlinePython @('-m', 'pip', 'check') 'offline proof pip check'

    $proof = [ordered]@{
        schema_version = 1
        inventory_path = $inventoryPath
        wheelhouse_manifest_path = $manifestPath
        wheelhouse_manifest_sha256 = Get-Sha256 $manifestPath
        offline_venv = $offlineVenv
        offline_install_summary = ($offlineOutput -split "`r?`n" | Select-Object -Last 20)
        pip_check_summary = ($pipCheckOutput -split "`r?`n" | Select-Object -Last 20)
    }
    Write-Utf8NoBom $offlineProofPath ($proof | ConvertTo-Json -Depth 8)

    [pscustomobject]@{
        status = 'PASS'
        inventory_path = $inventoryPath
        wheelhouse_manifest_path = $manifestPath
        wheelhouse_manifest_sha256 = Get-Sha256 $manifestPath
        offline_proof_path = $offlineProofPath
    } | ConvertTo-Json -Depth 6
} finally {
    $env:PIP_CACHE_DIR = $oldPipCacheDir
    $env:PIP_DISABLE_PIP_VERSION_CHECK = $oldPipDisableVersionCheck
}
