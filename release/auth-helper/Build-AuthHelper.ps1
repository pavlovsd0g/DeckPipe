[CmdletBinding()]
param([string]$SourceRoot=(Split-Path -Parent (Split-Path -Parent $PSScriptRoot)),[string]$TargetDirectory='D:\DeckPipe-RC-Lab\build\auth-broker-target',[switch]$Release)
$ErrorActionPreference='Stop'
$env:CARGO_HOME='D:\DeckPipe-RC-Lab\tool-cache\cargo-home'
$env:RUSTUP_HOME='D:\DeckPipe-RC-Lab\tool-cache\rustup-home'
$env:CARGO_TARGET_DIR=$TargetDirectory
$manifest=Join-Path $SourceRoot 'desktop\src-tauri\Cargo.toml'
$cargoArgs=@('build','--offline','--manifest-path',$manifest,'--bin','deckpipe-auth-host','--features','auth-host-only')
if ($Release) {$cargoArgs+='--release'}
& cargo @cargoArgs
if ($LASTEXITCODE -ne 0) {throw 'Native helper build failed.'}
$profile=if ($Release) {'release'} else {'debug'}
$source=Join-Path $TargetDirectory "$profile\deckpipe-auth-host.exe"
$destination=Join-Path $SourceRoot 'desktop\src-tauri\binaries\deckpipe-auth-host-x86_64-pc-windows-msvc.exe'
Copy-Item -LiteralPath $source -Destination $destination -Force
Write-Output 'Native helper staged for the Tauri build; no registration or browser installation performed.'
