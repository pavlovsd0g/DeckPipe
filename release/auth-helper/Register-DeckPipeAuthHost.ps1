[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][string]$InstallDirectory)
$ErrorActionPreference='Stop'
$install=(Resolve-Path -LiteralPath $InstallDirectory).Path
$hostExe=Join-Path $install 'deckpipe-auth-host.exe'
$appExe=Join-Path $install 'deckpipe.exe'
if (!(Test-Path -LiteralPath $hostExe -PathType Leaf) -or !(Test-Path -LiteralPath $appExe -PathType Leaf)) {throw 'DeckPipe and the native helper must both be installed in this directory.'}
$manifestPath=Join-Path $install 'native-host.firefox.json'
$key='HKCU:\Software\Mozilla\NativeMessagingHosts\com.deckpipe.auth'
$existing=(Get-Item -LiteralPath $key -ErrorAction SilentlyContinue)
if ($existing -and $existing.GetValue('') -ne $manifestPath) {throw 'Another DeckPipe installation owns this registration. Unregister that installation first.'}
if ($PSCmdlet.ShouldProcess($manifestPath,'Register the DeckPipe Firefox helper for the current Windows user')) {
    $manifest=[ordered]@{name='com.deckpipe.auth';description='DeckPipe browser authorization';path=$hostExe;type='stdio';allowed_extensions=@('deckpipe-auth@deckpipe.local')}
    $json=$manifest | ConvertTo-Json -Depth 3
    [IO.File]::WriteAllText($manifestPath,$json,(New-Object Text.UTF8Encoding($false)))
    $null=New-Item -Path $key -Force
    Set-Item -LiteralPath $key -Value $manifestPath
    Write-Output 'Firefox native helper registered for the current user. Extension installation is a separate user action.'
}
