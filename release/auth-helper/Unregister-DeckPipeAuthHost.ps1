[CmdletBinding(SupportsShouldProcess=$true)]
param([Parameter(Mandatory=$true)][string]$InstallDirectory)
$ErrorActionPreference='Stop'
$install=(Resolve-Path -LiteralPath $InstallDirectory).Path
$manifestPath=Join-Path $install 'native-host.firefox.json'
$key='HKCU:\Software\Mozilla\NativeMessagingHosts\com.deckpipe.auth'
$entry=Get-Item -LiteralPath $key -ErrorAction SilentlyContinue
if ($entry -and $entry.GetValue('') -ne $manifestPath) {throw 'Registration belongs to another installation; nothing was removed.'}
if ($PSCmdlet.ShouldProcess($manifestPath,'Remove only this installation Firefox native helper registration')) {
    if ($entry) {Remove-Item -LiteralPath $key}
    if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
        $manifest=Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
        if ($manifest.name -eq 'com.deckpipe.auth' -and $manifest.path -eq (Join-Path $install 'deckpipe-auth-host.exe')) {Remove-Item -LiteralPath $manifestPath}
    }
    Write-Output 'Matching helper registration removed. Firefox extension and credentials were not changed.'
}
