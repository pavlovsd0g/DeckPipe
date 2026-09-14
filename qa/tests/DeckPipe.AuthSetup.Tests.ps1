$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:Passed = 0
$script:Failed = 0
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$register = Join-Path $repoRoot 'release\auth-helper\Register-DeckPipeAuthHost.ps1'
$unregister = Join-Path $repoRoot 'release\auth-helper\Unregister-DeckPipeAuthHost.ps1'
$script:HostKey = 'HKCU:\Software\Mozilla\NativeMessagingHosts\com.deckpipe.auth'

function Assert-True { param([bool]$Condition, [string]$Message = 'Expected true') if (-not $Condition) { throw $Message } }
function Assert-Equal { param($Actual, $Expected, [string]$Message = '') if ($Actual -ne $Expected) { throw "$Message Expected '$Expected', got '$Actual'" } }
function Assert-Throws { param([scriptblock]$Body, [string]$Pattern) try { & $Body } catch { if ($_.Exception.Message -match $Pattern) { return }; throw "Expected '$Pattern', got '$($_.Exception.Message)'" }; throw "Expected error '$Pattern'" }
function It { param([string]$Name, [scriptblock]$Body) try { & $Body; $script:Passed++; Write-Host "PASS $Name" } catch { $script:Failed++; Write-Host "FAIL $Name :: $($_.Exception.Message)" } }

function Install-RegistryMock {
    $global:DeckPipeAuthSetupRegistry = [ordered]@{ Present = $false; Value = $null; FailSet = $false; FailSetNonTerminating = $false; FailRemoveNonTerminating = $false; FailRemove = $false; Calls = [Collections.Generic.List[string]]::new() }
    function global:Get-Item {
        [CmdletBinding()] param([string]$LiteralPath)
        if ($LiteralPath -eq $global:DeckPipeAuthSetupHostKey) {
            $global:DeckPipeAuthSetupRegistry.Calls.Add("Get:$LiteralPath")
            if (-not $global:DeckPipeAuthSetupRegistry.Present) { return $null }
            $entry = [pscustomobject]@{}
            $entry | Add-Member -MemberType ScriptMethod -Name GetValue -Value { param($Name) $global:DeckPipeAuthSetupRegistry.Value }
            return $entry
        }
        return Microsoft.PowerShell.Management\Get-Item @PSBoundParameters
    }
    function global:New-Item {
        [CmdletBinding()] param([string]$Path, [switch]$Force)
        if ($Path -eq $global:DeckPipeAuthSetupHostKey) {
            $global:DeckPipeAuthSetupRegistry.Calls.Add("New:$Path")
            $global:DeckPipeAuthSetupRegistry.Present = $true
            $global:DeckPipeAuthSetupRegistry.Value = $null
            return [pscustomobject]@{}
        }
        return Microsoft.PowerShell.Management\New-Item @PSBoundParameters
    }
    function global:Set-Item {
        [CmdletBinding()] param([string]$LiteralPath, [string]$Value)
        if ($LiteralPath -eq $global:DeckPipeAuthSetupHostKey) {
            $global:DeckPipeAuthSetupRegistry.Calls.Add("Set:$LiteralPath=$Value")
            if ($global:DeckPipeAuthSetupRegistry.FailSet) { throw 'synthetic registry publication failure' }
            if ($global:DeckPipeAuthSetupRegistry.FailSetNonTerminating) { Write-Error 'synthetic nonterminating registry publication failure'; return }
            $global:DeckPipeAuthSetupRegistry.Present = $true
            $global:DeckPipeAuthSetupRegistry.Value = $Value
            return
        }
        return Microsoft.PowerShell.Management\Set-Item @PSBoundParameters
    }
    function global:Remove-Item {
        [CmdletBinding()] param([string]$LiteralPath, [switch]$Force)
        if ($LiteralPath -eq $global:DeckPipeAuthSetupHostKey) {
            $global:DeckPipeAuthSetupRegistry.Calls.Add("Remove:$LiteralPath")
            if ($global:DeckPipeAuthSetupRegistry.FailRemove) { throw 'synthetic registry Delete denied' }
            if ($global:DeckPipeAuthSetupRegistry.FailRemoveNonTerminating) { Write-Error 'synthetic nonterminating registry removal failure'; return }
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            return
        }
        return Microsoft.PowerShell.Management\Remove-Item @PSBoundParameters
    }
}

function Uninstall-RegistryMock {
    foreach ($name in 'Get-Item','New-Item','Set-Item','Remove-Item') { Microsoft.PowerShell.Management\Remove-Item -LiteralPath "Function:\global:$name" -Force }
    Remove-Variable -Name DeckPipeAuthSetupRegistry -Scope Global -Force -ErrorAction SilentlyContinue
}

function New-SetupFixture {
    $root = Join-Path ([IO.Path]::GetTempPath()) ('deckpipe-auth-setup-' + [guid]::NewGuid().ToString('N'))
    $install = Join-Path $root 'Program Files\DeckPipe'
    $local = Join-Path $root 'LocalAppData'
    [IO.Directory]::CreateDirectory($install) | Out-Null
    [IO.Directory]::CreateDirectory($local) | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $install 'deckpipe.exe'), [byte[]](0))
    [IO.File]::WriteAllBytes((Join-Path $install 'deckpipe-auth-host.exe'), [byte[]](0))
    [pscustomobject]@{ Root = $root; Install = $install; Local = $local; Manifest = (Join-Path $local 'DeckPipe\AuthHelper\native-host.firefox.json') }
}

$global:DeckPipeAuthSetupHostKey = $script:HostKey
Install-RegistryMock
try {
    It 'registers the manifest only in isolated LocalAppData' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            & $register -InstallDirectory $fixture.Install
            Assert-True (Test-Path -LiteralPath $fixture.Manifest) 'Expected the per-user manifest'
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture.Install 'native-host.firefox.json'))) 'Install directory must remain manifest-free'
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'unregisters a matching per-user manifest after the installed directory was removed' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            [IO.Directory]::CreateDirectory((Split-Path -Parent $fixture.Manifest)) | Out-Null
            $manifest = [ordered]@{ name = 'com.deckpipe.auth'; type = 'stdio'; path = (Join-Path $fixture.Install 'deckpipe-auth-host.exe'); allowed_extensions = @('deckpipe-auth@deckpipe.local') } | ConvertTo-Json -Depth 3
            [IO.File]::WriteAllText($fixture.Manifest, $manifest, [Text.UTF8Encoding]::new($false))
            $global:DeckPipeAuthSetupRegistry.Present = $true
            $global:DeckPipeAuthSetupRegistry.Value = $fixture.Manifest
            $sentinel = Join-Path $fixture.Local 'DeckPipe\AuthHelper\unrelated.txt'
            [IO.File]::WriteAllText($sentinel, 'retain')
            [IO.Directory]::Delete($fixture.Install, $true)
            & $unregister -InstallDirectory $fixture.Install
            Assert-True (-not $global:DeckPipeAuthSetupRegistry.Present)
            Assert-True (-not (Test-Path -LiteralPath $fixture.Manifest))
            Assert-True (Test-Path -LiteralPath $sentinel)
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'keeps the original manifest and registry when another installation registers' {
        $first = New-SetupFixture
        $second = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $first.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $first.Install
            $original = [IO.File]::ReadAllBytes($first.Manifest)
            $env:LOCALAPPDATA = $first.Local
            Assert-Throws { & $register -InstallDirectory $second.Install } 'Another DeckPipe installation|belongs to another installation'
            Assert-Equal (([IO.File]::ReadAllBytes($first.Manifest) -join ',') -join '') (($original -join ',') -join '')
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $first.Manifest
        } finally {
            $env:LOCALAPPDATA = $saved
            foreach ($fixture in @($first, $second)) { if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) } }
        }
    }

    It 'refuses unregister from another installation when the fixed manifest belongs to the first' {
        $first = New-SetupFixture
        $second = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $first.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $first.Install
            $before = [IO.File]::ReadAllBytes($first.Manifest)
            Assert-Throws { & $unregister -InstallDirectory $second.Install } 'per-user manifest is not owned by this installation'
            Assert-Equal (([IO.File]::ReadAllBytes($first.Manifest) -join ',') -join '') (($before -join ',') -join '')
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $first.Manifest
        } finally {
            $env:LOCALAPPDATA = $saved
            foreach ($fixture in @($first, $second)) { if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) } }
        }
    }

    It 'makes WhatIf leave the per-user manifest and registry untouched' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install -WhatIf
            Assert-True (-not (Test-Path -LiteralPath $fixture.Manifest))
            Assert-True (-not $global:DeckPipeAuthSetupRegistry.Present)
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'keeps a successful repeated registration unchanged' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install
            $before = [IO.File]::ReadAllBytes($fixture.Manifest)
            & $register -InstallDirectory $fixture.Install
            Assert-Equal (([IO.File]::ReadAllBytes($fixture.Manifest) -join ',') -join '') (($before -join ',') -join '')
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'makes unregister WhatIf retain the matching registration and manifest' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install
            & $unregister -InstallDirectory $fixture.Install -WhatIf
            Assert-True (Test-Path -LiteralPath $fixture.Manifest)
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'migrates a validated legacy registration without deleting its install-side manifest' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $legacy = Join-Path $fixture.Install 'native-host.firefox.json'
            $legacyValue = [ordered]@{ name = 'com.deckpipe.auth'; type = 'stdio'; path = (Join-Path $fixture.Install 'deckpipe-auth-host.exe'); allowed_extensions = @('deckpipe-auth@deckpipe.local') } | ConvertTo-Json -Depth 3
            [IO.File]::WriteAllText($legacy, $legacyValue, [Text.UTF8Encoding]::new($false))
            $global:DeckPipeAuthSetupRegistry.Present = $true
            $global:DeckPipeAuthSetupRegistry.Value = $legacy
            & $register -InstallDirectory $fixture.Install
            Assert-True (Test-Path -LiteralPath $legacy)
            Assert-True (Test-Path -LiteralPath $fixture.Manifest)
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'restores the previous per-user manifest when registry publication fails' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install
            $before = [IO.File]::ReadAllBytes($fixture.Manifest)
            $global:DeckPipeAuthSetupRegistry.FailSet = $true
            Assert-Throws { & $register -InstallDirectory $fixture.Install } 'synthetic registry publication failure'
            Assert-Equal (([IO.File]::ReadAllBytes($fixture.Manifest) -join ',') -join '') (($before -join ',') -join '')
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
        } finally {
            $global:DeckPipeAuthSetupRegistry.FailSet = $false
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'under caller Continue treats registry publication errors as failure and preserves repeat registration' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install
            $before = [IO.File]::ReadAllBytes($fixture.Manifest)
            $global:DeckPipeAuthSetupRegistry.FailSetNonTerminating = $true
            Assert-Throws { & { $ErrorActionPreference = 'Continue'; & $register -InstallDirectory $fixture.Install } } 'synthetic nonterminating registry publication failure'
            Assert-Equal (([IO.File]::ReadAllBytes($fixture.Manifest) -join ',') -join '') (($before -join ',') -join '')
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
        } finally {
            $global:DeckPipeAuthSetupRegistry.FailSetNonTerminating = $false
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'under caller Continue preserves the manifest when registry removal errors' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install
            $global:DeckPipeAuthSetupRegistry.FailRemoveNonTerminating = $true
            Assert-Throws { & { $ErrorActionPreference = 'Continue'; & $unregister -InstallDirectory $fixture.Install } } 'synthetic nonterminating registry removal failure'
            Assert-True (Test-Path -LiteralPath $fixture.Manifest)
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $fixture.Manifest
        } finally {
            $global:DeckPipeAuthSetupRegistry.FailRemoveNonTerminating = $false
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'restores an existing manifest when first publication and key cleanup both fail' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            [IO.Directory]::CreateDirectory((Split-Path -Parent $fixture.Manifest)) | Out-Null
            $owned = [ordered]@{ name = 'com.deckpipe.auth'; type = 'stdio'; path = (Join-Path $fixture.Install 'deckpipe-auth-host.exe'); allowed_extensions = @('deckpipe-auth@deckpipe.local') } | ConvertTo-Json -Depth 3
            [IO.File]::WriteAllText($fixture.Manifest, $owned, [Text.UTF8Encoding]::new($false))
            $before = [IO.File]::ReadAllBytes($fixture.Manifest)
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            $global:DeckPipeAuthSetupRegistry.FailSet = $true
            $global:DeckPipeAuthSetupRegistry.FailRemove = $true
            Assert-Throws { & $register -InstallDirectory $fixture.Install } 'synthetic registry publication failure; rollback cleanup failed: synthetic registry Delete denied'
            Assert-Equal (([IO.File]::ReadAllBytes($fixture.Manifest) -join ',') -join '') (($before -join ',') -join '')
        } finally {
            $global:DeckPipeAuthSetupRegistry.FailSet = $false
            $global:DeckPipeAuthSetupRegistry.FailRemove = $false
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'registers from an ACL-denied synthetic install directory without writing there' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
        try {
            $env:LOCALAPPDATA = $fixture.Local
            & icacls $fixture.Install /deny "${identity}:(W)" | Out-Null
            Assert-Throws { [IO.File]::WriteAllText((Join-Path $fixture.Install 'write-probe.txt'), 'blocked') } 'access|denied'
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            & $register -InstallDirectory $fixture.Install
            Assert-True (Test-Path -LiteralPath $fixture.Manifest)
            Assert-True (-not (Test-Path -LiteralPath (Join-Path $fixture.Install 'native-host.firefox.json')))
        } finally {
            & icacls $fixture.Install /remove:d $identity | Out-Null
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'removes transaction-owned state after failed first publication' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $global:DeckPipeAuthSetupRegistry.Present = $false
            $global:DeckPipeAuthSetupRegistry.Value = $null
            $global:DeckPipeAuthSetupRegistry.FailSet = $true
            Assert-Throws { & $register -InstallDirectory $fixture.Install } 'synthetic registry publication failure'
            Assert-True (-not (Test-Path -LiteralPath $fixture.Manifest))
            Assert-True (-not $global:DeckPipeAuthSetupRegistry.Present)
        } finally {
            $global:DeckPipeAuthSetupRegistry.FailSet = $false
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'preserves invalid legacy ownership and unrelated state on refused unregister' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $legacy = Join-Path $fixture.Install 'native-host.firefox.json'
            [IO.File]::WriteAllText($legacy, '{}')
            $sentinel = Join-Path $fixture.Local 'DeckPipe\AuthHelper\unrelated.txt'
            [IO.Directory]::CreateDirectory((Split-Path -Parent $sentinel)) | Out-Null
            [IO.File]::WriteAllText($sentinel, 'retain')
            $global:DeckPipeAuthSetupRegistry.Present = $true
            $global:DeckPipeAuthSetupRegistry.Value = $legacy
            Assert-Throws { & $register -InstallDirectory $fixture.Install } 'Another DeckPipe installation owns'
            Assert-Throws { & $unregister -InstallDirectory $fixture.Install } 'Registration belongs to another installation'
            Assert-True (Test-Path -LiteralPath $legacy)
            Assert-True (Test-Path -LiteralPath $sentinel)
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $legacy
        } finally {
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }

    It 'preserves validated legacy registration when migration publication fails' {
        $fixture = New-SetupFixture
        $saved = $env:LOCALAPPDATA
        try {
            $env:LOCALAPPDATA = $fixture.Local
            $legacy = Join-Path $fixture.Install 'native-host.firefox.json'
            $legacyValue = [ordered]@{ name = 'com.deckpipe.auth'; type = 'stdio'; path = (Join-Path $fixture.Install 'deckpipe-auth-host.exe'); allowed_extensions = @('deckpipe-auth@deckpipe.local') } | ConvertTo-Json -Depth 3
            [IO.File]::WriteAllText($legacy, $legacyValue, [Text.UTF8Encoding]::new($false))
            $before = [IO.File]::ReadAllBytes($legacy)
            $global:DeckPipeAuthSetupRegistry.Present = $true
            $global:DeckPipeAuthSetupRegistry.Value = $legacy
            $global:DeckPipeAuthSetupRegistry.FailSet = $true
            Assert-Throws { & $register -InstallDirectory $fixture.Install } 'synthetic registry publication failure'
            Assert-Equal (([IO.File]::ReadAllBytes($legacy) -join ',') -join '') (($before -join ',') -join '')
            Assert-Equal $global:DeckPipeAuthSetupRegistry.Value $legacy
            Assert-True (-not (Test-Path -LiteralPath $fixture.Manifest))
        } finally {
            $global:DeckPipeAuthSetupRegistry.FailSet = $false
            $env:LOCALAPPDATA = $saved
            if (Test-Path -LiteralPath $fixture.Root) { [IO.Directory]::Delete($fixture.Root, $true) }
        }
    }
} finally {
    Uninstall-RegistryMock
}

Write-Host "RESULT passed=$script:Passed failed=$script:Failed"
if ($script:Failed -gt 0) { exit 1 }
