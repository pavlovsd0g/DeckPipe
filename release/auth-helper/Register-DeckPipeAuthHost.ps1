[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][string]$InstallDirectory)
$ErrorActionPreference = 'Stop'
function Get-DeckPipeAuthManifestPath {
    $localAppData = [Environment]::GetEnvironmentVariable('LOCALAPPDATA', 'Process')
    if ([string]::IsNullOrWhiteSpace($localAppData) -or -not [IO.Path]::IsPathRooted($localAppData)) { throw 'LOCALAPPDATA must be an absolute per-user path.' }
    Join-Path $localAppData 'DeckPipe\AuthHelper\native-host.firefox.json'
}
function Get-DeckPipeAuthInstall {
    param([string]$Path, [switch]$RequirePresent)
    if (-not [IO.Path]::IsPathRooted($Path)) { throw 'InstallDirectory must be an absolute path.' }
    $full = [IO.Path]::GetFullPath($Path)
    if ($RequirePresent) {
        $full = (Resolve-Path -LiteralPath $full -ErrorAction Stop).Path
        foreach ($file in 'deckpipe.exe','deckpipe-auth-host.exe') { if (-not (Test-Path -LiteralPath (Join-Path $full $file) -PathType Leaf)) { throw 'DeckPipe and the native helper must both be installed in this directory.' } }
    }
    $full.TrimEnd('\')
}
function Test-DeckPipeAuthManifestOwner {
    param([string]$ManifestPath, [string]$HostExe)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) { return $false }
    try {
        $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json -ErrorAction Stop
        $configured = [string]$manifest.path
        return $manifest.name -eq 'com.deckpipe.auth' -and $manifest.type -eq 'stdio' -and @($manifest.allowed_extensions).Count -eq 1 -and @($manifest.allowed_extensions)[0] -eq 'deckpipe-auth@deckpipe.local' -and [IO.Path]::GetFullPath($configured).TrimEnd('\') -ieq [IO.Path]::GetFullPath($HostExe).TrimEnd('\')
    } catch { return $false }
}
function Set-DeckPipeAuthManifestBytes {
    param([string]$ManifestPath, [byte[]]$Bytes)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $ManifestPath)) | Out-Null
    $temp = "$ManifestPath.$([guid]::NewGuid().ToString('N')).tmp"
    $backup = "$temp.backup"
    try {
        [IO.File]::WriteAllBytes($temp, $Bytes)
        if (Test-Path -LiteralPath $ManifestPath -PathType Leaf) { [IO.File]::Replace($temp, $ManifestPath, $backup) } else { [IO.File]::Move($temp, $ManifestPath) }
    } finally {
        if (Test-Path -LiteralPath $temp -PathType Leaf) { Remove-Item -LiteralPath $temp -Force }
        if (Test-Path -LiteralPath $backup -PathType Leaf) { Remove-Item -LiteralPath $backup -Force }
    }
}
$install = Get-DeckPipeAuthInstall -Path $InstallDirectory -RequirePresent
$hostExe = Join-Path $install 'deckpipe-auth-host.exe'
$manifestPath = Get-DeckPipeAuthManifestPath
$legacyManifest = Join-Path $install 'native-host.firefox.json'
$key = 'HKCU:\Software\Mozilla\NativeMessagingHosts\com.deckpipe.auth'
$entry = Get-Item -LiteralPath $key -ErrorAction SilentlyContinue
$registeredPath = if ($entry) {[string]$entry.GetValue('')} else {''}
$legacyOwned = $registeredPath -and $registeredPath -ieq $legacyManifest -and (Test-DeckPipeAuthManifestOwner -ManifestPath $legacyManifest -HostExe $hostExe)
if ($entry -and $registeredPath -ine $manifestPath -and -not $legacyOwned) { throw 'Another DeckPipe installation owns this registration. Unregister that installation first.' }
if ((Test-Path -LiteralPath $manifestPath -PathType Leaf) -and -not (Test-DeckPipeAuthManifestOwner -ManifestPath $manifestPath -HostExe $hostExe)) { throw 'The per-user DeckPipe manifest belongs to another installation or is invalid.' }
if ($PSCmdlet.ShouldProcess($manifestPath, 'Register the DeckPipe Firefox helper for the current Windows user')) {
    $hadManifest = Test-Path -LiteralPath $manifestPath -PathType Leaf
    $previousBytes = if ($hadManifest) {[IO.File]::ReadAllBytes($manifestPath)} else {$null}
    $createdKey = $false
    try {
        $value = [ordered]@{ name = 'com.deckpipe.auth'; description = 'DeckPipe browser authorization'; path = $hostExe; type = 'stdio'; allowed_extensions = @('deckpipe-auth@deckpipe.local') }
        Set-DeckPipeAuthManifestBytes -ManifestPath $manifestPath -Bytes ([Text.UTF8Encoding]::new($false).GetBytes(($value | ConvertTo-Json -Depth 3)))
        if (-not $entry) { $null = New-Item -Path $key -Force; $createdKey = $true }
        Set-Item -LiteralPath $key -Value $manifestPath
    } catch {
        $publicationFailure = $_
        $cleanupFailure = $null
        try { if ($createdKey) { Remove-Item -LiteralPath $key -Force } } catch { $cleanupFailure = $_ }
        try {
            if ($hadManifest) { Set-DeckPipeAuthManifestBytes -ManifestPath $manifestPath -Bytes $previousBytes }
            elseif (Test-Path -LiteralPath $manifestPath -PathType Leaf) { Remove-Item -LiteralPath $manifestPath -Force }
        } catch {
            if ($null -eq $cleanupFailure) { $cleanupFailure = $_ }
        }
        if ($cleanupFailure) { throw "$($publicationFailure.Exception.Message); rollback cleanup failed: $($cleanupFailure.Exception.Message)" }
        throw $publicationFailure
    }
    Write-Output 'Firefox native helper registered for the current user. Extension installation is a separate user action.'
}
