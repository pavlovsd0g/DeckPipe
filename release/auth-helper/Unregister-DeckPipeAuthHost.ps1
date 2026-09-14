[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][string]$InstallDirectory)
$ErrorActionPreference = 'Stop'
function Get-DeckPipeAuthManifestPath {
    $localAppData = [Environment]::GetEnvironmentVariable('LOCALAPPDATA', 'Process')
    if ([string]::IsNullOrWhiteSpace($localAppData) -or -not [IO.Path]::IsPathRooted($localAppData)) { throw 'LOCALAPPDATA must be an absolute per-user path.' }
    Join-Path $localAppData 'DeckPipe\AuthHelper\native-host.firefox.json'
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
if (-not [IO.Path]::IsPathRooted($InstallDirectory)) { throw 'InstallDirectory must be an absolute path.' }
$install = [IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
$hostExe = Join-Path $install 'deckpipe-auth-host.exe'
$manifestPath = Get-DeckPipeAuthManifestPath
$key = 'HKCU:\Software\Mozilla\NativeMessagingHosts\com.deckpipe.auth'
$entry = Get-Item -LiteralPath $key -ErrorAction SilentlyContinue
if ($entry -and [string]$entry.GetValue('') -ine $manifestPath) { throw 'Registration belongs to another installation; nothing was removed.' }
if (-not (Test-DeckPipeAuthManifestOwner -ManifestPath $manifestPath -HostExe $hostExe)) { throw 'The per-user manifest is not owned by this installation; nothing was removed.' }
if ($PSCmdlet.ShouldProcess($manifestPath, 'Remove only this installation Firefox native helper registration')) {
    if ($entry) { Remove-Item -LiteralPath $key }
    Remove-Item -LiteralPath $manifestPath -Force
    Write-Output 'Matching helper registration removed. Firefox extension and credentials were not changed.'
}
