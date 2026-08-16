#Requires -Version 5.1
<#
.SYNOPSIS
    Install/uninstall the HSEM agent relay as a per-user Scheduled Task.

.DESCRIPTION
    The relay bridges YubiKey GPG/SSH agent endpoints over TCP so Docker
    containers can access them (Docker Desktop cannot forward Windows
    named pipes or GnuPG emulated sockets into containers).

    Install registers an auto-start mechanism for the relay: a per-user
    Scheduled Task (restarts on failure) when policy allows it, otherwise
    a hidden launcher in the user's Startup folder. It also stages the host
    .ssh and .gitconfig files into .devcontainer/host-config and snapshots
    the public GnuPG key material (%APPDATA%\gnupg) into
    .devcontainer/gnupg-host so the container can import public keys; rerun
    install after changing SSH/Git config or adding keys.

    No administrator rights are required.

.PARAMETER Action
    install    - Register and start the relay task (run once, or to
                 refresh the gnupg snapshot).
    uninstall  - Stop and remove the task and copied files.
    status     - Show task state and listening ports.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File .devcontainer\scripts\install-agent-relay.ps1 install
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position = 0)]
    [ValidateSet('install', 'uninstall', 'status')]
    [string]$Action
)

$ErrorActionPreference = 'Stop'

$TaskName = 'HSEM Agent Relay'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RelaySrc = Join-Path $ScriptDir 'start-agent-relay.ps1'
$HostConfigStage = Join-Path $ScriptDir 'initialize-host.ps1'
$InstallDir = Join-Path $env:APPDATA 'hsem-agent-relay'
$RelayDst = Join-Path $InstallDir 'start-agent-relay.ps1'
$LogFile = Join-Path $InstallDir 'agent-relay.log'
$StartupVbs = Join-Path ([Environment]::GetFolderPath('Startup')) 'hsem-agent-relay.vbs'
$WorkspaceRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
$GpgSnapshot = Join-Path $WorkspaceRoot '.devcontainer\gnupg-host'
$GpgExportFile = Join-Path $InstallDir 'public-keys.asc'

function Install-Relay {
    Write-Host 'Installing HSEM agent relay as a Scheduled Task...'

    if (-not (Test-Path $RelaySrc)) {
        Write-Host "ERROR: Relay script not found at $RelaySrc"
        exit 1
    }
    if (-not (Get-Command gpgconf.exe -ErrorAction SilentlyContinue)) {
        Write-Host 'WARNING: gpgconf not found in PATH. Install Gpg4win and ensure'
        Write-Host '         gpg-agent.conf contains "enable-win32-openssh-support".'
    }
    if (-not (Get-Command gpg.exe -ErrorAction SilentlyContinue)) {
        Write-Host 'ERROR: gpg.exe not found in PATH.'
        exit 1
    }

    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null

    # Stop any already-running relay (it may hold an older script version
    # in memory or stale connections to the agents).
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like '*hsem-agent-relay*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }

    Copy-Item $RelaySrc $RelayDst -Force

    if (Test-Path $HostConfigStage) {
        & $HostConfigStage
    } else {
        Write-Host "WARNING: Host config staging script not found at $HostConfigStage"
    }

    # Snapshot GnuPG public key material for the container bridge.
    $gpgHome = Join-Path $env:APPDATA 'gnupg'
    if (Test-Path $gpgHome) {
        if (Test-Path $GpgSnapshot) { Remove-Item $GpgSnapshot -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $GpgSnapshot | Out-Null
        Remove-Item $GpgExportFile -Force -ErrorAction SilentlyContinue
        & gpg.exe --homedir $gpgHome --batch --yes --armor --export |
            Set-Content -Path $GpgExportFile -Encoding ASCII
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to export public keys from the host GnuPG home.'
        }
        & gpg.exe --homedir $GpgSnapshot --batch --yes --import $GpgExportFile | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to import exported public keys into the container snapshot.'
        }
        Remove-Item $GpgExportFile -Force -ErrorAction SilentlyContinue
        Write-Host "Snapshot of $gpgHome -> $GpgSnapshot"
    } else {
        Write-Host "WARNING: GnuPG home not found at $gpgHome; skipping snapshot."
    }

    $psExe = Join-Path $PSHOME 'powershell.exe'
    $taskArgs = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$RelayDst`""
    $installed = $false

    # Preferred: per-user Scheduled Task (restarts the relay on failure).
    # Some corporate policies deny task creation; fall back to a Startup
    # folder launcher, which needs no special rights.
    try {
        $action = New-ScheduledTaskAction -Execute $psExe -Argument $taskArgs `
            -WorkingDirectory $InstallDir
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)

        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false `
            -ErrorAction SilentlyContinue
        Register-ScheduledTask -TaskName $TaskName -Action $action `
            -Trigger $trigger -Settings $settings `
            -Description 'TCP relays for YubiKey SSH/GPG agents into Docker devcontainers' `
            -ErrorAction Stop | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        $installed = $true
        Write-Host "  Auto-start:   Scheduled Task '$TaskName' (starts at logon, restarts on failure)"
    } catch {
        Write-Host "Scheduled Task unavailable ($($_.Exception.Message));"
        Write-Host 'falling back to Startup folder launcher.'
    }

    if (-not $installed) {
        # Hidden launcher: wscript runs the relay without a console window.
        # For troubleshooting, run start-agent-relay.ps1 manually in a
        # visible console to see its log output.
        $vbs = @"
Set sh = CreateObject("Wscript.Shell")
sh.CurrentDirectory = "$InstallDir"
sh.Run """$psExe"" -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File ""$RelayDst""", 0, False
"@
        Set-Content -Path $StartupVbs -Value $vbs -Encoding ASCII
        # Start it now (same as at next logon).
        Start-Process wscript.exe -ArgumentList "`"$StartupVbs`""
        Write-Host "  Auto-start:   Startup launcher $StartupVbs"
    }

    Write-Host ''
    Write-Host 'Installed. The relay starts automatically at logon.'
    Write-Host "  Relay script: $RelayDst"
    Write-Host ''
    Write-Host 'Ports: 9999 (SSH agent), 9998 (GPG agent), 9997 (scdaemon)'
}

function Uninstall-Relay {
    Write-Host 'Uninstalling HSEM agent relay...'
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false `
        -ErrorAction SilentlyContinue
    Remove-Item $StartupVbs -Force -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.CommandLine -like '*hsem-agent-relay*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
    Remove-Item $InstallDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host 'Uninstalled.'
    Write-Host "Note: the gnupg snapshot at $GpgSnapshot was left in place; delete it manually if unwanted."
}

function Get-RelayStatus {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    $startup = Test-Path $StartupVbs
    if ($task) {
        Write-Host "Service: Scheduled Task ($($task.State))"
    } elseif ($startup) {
        Write-Host "Service: Startup launcher installed ($StartupVbs)"
    } else {
        Write-Host 'Service: NOT INSTALLED'
    }
    $listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalPort -in 9997, 9998, 9999 }
    if ($listeners) {
        $listeners | ForEach-Object {
            Write-Host "Listening: $($_.LocalAddress):$($_.LocalPort) (PID $($_.OwningProcess))"
        }
    } else {
        Write-Host 'No relay ports listening (may need a moment to start)'
    }
}

switch ($Action) {
    'install' { Install-Relay }
    'uninstall' { Uninstall-Relay }
    'status' { Get-RelayStatus }
}
