#Requires -Version 5.1
<#
.SYNOPSIS
    Start TCP relays for SSH agent, GPG agent, and scdaemon on a Windows host.

.DESCRIPTION
    Docker Desktop for Windows runs inside a Linux VM and cannot reach
    Windows named pipes or GnuPG's emulated sockets from a container.
    This script listens on TCP ports (localhost only) and forwards traffic
    to the host agents, allowing the devcontainer to use the YubiKey.

    Backends:
      SSH agent  -> \\.\pipe\openssh-ssh-agent named pipe, served by
                    gpg-agent when gpg-agent.conf contains
                    "enable-win32-openssh-support" (Gpg4win).
      GPG agent  -> GnuPG socket-emulation file (S.gpg-agent): the file
                    contains a localhost TCP port and a 16-byte nonce that
                    must be sent immediately after connecting.
      scdaemon   -> Same mechanism (S.scdaemon), for direct card access
                    (e.g. ykman / gpg --card-status from the container).

    GnuPG socket paths are resolved via "gpgconf --list-dirs" with a
    fallback to %LOCALAPPDATA%\gnupg. The port and nonce are re-read for
    every connection because gpg-agent rewrites them when it restarts.

    Concurrency model: one runspace per relay accept loop, plus one
    runspace per accepted connection. ThreadPool scriptblock callbacks
    crash under Windows PowerShell 5.1 -File, and .NET CopyToAsync avoids
    needing PowerShell callbacks at all. If a relay dies, the watchdog
    exits with an error so a supervising Scheduled Task can restart it.

.PARAMETER SshPort
    TCP port for the SSH agent relay (default 9999).

.PARAMETER GpgPort
    TCP port for the GPG agent relay (default 9998).

.PARAMETER ScdaemonPort
    TCP port for the scdaemon relay (default 9997).

.EXAMPLE
    powershell -NoProfile -File start-agent-relay.ps1
#>
[CmdletBinding()]
param(
    [int]$SshPort = 9999,
    [int]$GpgPort = 9998,
    [int]$ScdaemonPort = 9997
)

$ErrorActionPreference = 'Stop'

function Write-Log {
    param([string]$Message)
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    [Console]::WriteLine("[$ts] $Message")
}

function Get-GpgconfDirs {
    # Parse "gpgconf --list-dirs" into a hashtable (entry name -> path).
    $dirs = @{}
    $gpgconf = Get-Command gpgconf.exe -ErrorAction SilentlyContinue
    if ($gpgconf) {
        try {
            & $gpgconf.Source --list-dirs 2>$null | ForEach-Object {
                $sep = $_.IndexOf(':')
                if ($sep -gt 0) {
                    $dirs[$_.Substring(0, $sep)] =
                        [Uri]::UnescapeDataString($_.Substring($sep + 1))
                }
            }
        } catch {
            Write-Log "gpgconf --list-dirs failed: $_"
        }
    }
    if (-not $dirs.ContainsKey('socketdir')) {
        $dirs['socketdir'] = Join-Path $env:LOCALAPPDATA 'gnupg'
    }
    return $dirs
}

# Runs a single accepted connection in its own runspace: pumps bytes in
# both directions with CopyToAsync and closes everything when either side
# disconnects. WaitAny (not WaitAll) is essential: the agent protocols are
# persistent, so when the client goes away the backend->client pump would
# otherwise block forever and leak the gpg-agent connection, eventually
# stalling the agent's pipe server.
# State: @(Client, BackendStream, BackendCloseTarget).
$ConnectionHandlerSource = @'
param($s)
$client = $s[0]; $backendStream = $s[1]; $backendTarget = $s[2]
try {
    $cs = $client.GetStream()
    $t1 = $cs.CopyToAsync($backendStream)
    $t2 = $backendStream.CopyToAsync($cs)
    [void][System.Threading.Tasks.Task]::WaitAny(@($t1, $t2))
} catch {
    [Console]::Error.WriteLine("connection handler error: $_")
} finally {
    try { $backendTarget.Close() } catch {}
    try { $client.Close() } catch {}
}
'@

# TCP listener -> Windows named pipe (SSH agent protocol).
$PipeRelaySource = @'
param([int]$ListenPort, [string]$PipeName, [string]$Label, [string]$HandlerSource)

$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback, $ListenPort)
$listener.Start()
[Console]::WriteLine("$Label relay: 127.0.0.1:$ListenPort -> \\.\pipe\$PipeName")

$handlers = @()
while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        # PipeOptions.Asynchronous is required: the connection handler
        # closes the pipe while a pump read is still pending, and only
        # overlapped IO cancels cleanly. Without it the blocking read
        # on a synchronous handle wedges gpg-agent's pipe server.
        $pipe = [System.IO.Pipes.NamedPipeClientStream]::new(
            '.', $PipeName, [System.IO.Pipes.PipeDirection]::InOut,
            [System.IO.Pipes.PipeOptions]::Asynchronous)
        $pipe.Connect(5000)
    } catch {
        [Console]::Error.WriteLine("$Label relay: pipe connect failed: $_")
        $client.Close()
        continue
    }

    $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace(
        [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault())
    $rs.Open()
    $ps = [System.Management.Automation.PowerShell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($HandlerSource).AddArgument(@($client, $pipe, $pipe))
    [void]$ps.BeginInvoke()
    $handlers += @{ PS = $ps; RS = $rs }

    # Reap finished connection handlers.
    $alive = @()
    foreach ($h in $handlers) {
        if ($h.PS.InvocationStateInfo.State -eq 'Running') {
            $alive += $h
        } else {
            try { $h.PS.EndInvoke($null) } catch {}
            $h.PS.Dispose(); $h.RS.Close(); $h.RS.Dispose()
        }
    }
    $handlers = $alive
}
'@

# TCP listener -> GnuPG emulated socket (localhost TCP port + nonce).
$GpgRelaySource = @'
param([int]$ListenPort, [string]$SocketFile, [string]$Label, [string]$HandlerSource)

$listener = [System.Net.Sockets.TcpListener]::new(
    [System.Net.IPAddress]::Loopback, $ListenPort)
$listener.Start()
[Console]::WriteLine("$Label relay: 127.0.0.1:$ListenPort -> $SocketFile")

$handlers = @()
while ($true) {
    $client = $listener.AcceptTcpClient()
    try {
        # Re-read port + nonce per connection: gpg-agent rewrites the
        # file when it restarts.
        $bytes = [System.IO.File]::ReadAllBytes($SocketFile)
        $nl = [Array]::IndexOf($bytes, [byte]10)
        if ($nl -lt 1 -or $bytes.Length -le ($nl + 1)) {
            throw 'unrecognized socket file format'
        }
        $port = [int][System.Text.Encoding]::ASCII.GetString($bytes, 0, $nl)
        $nonce = New-Object byte[] ($bytes.Length - $nl - 1)
        [Array]::Copy($bytes, $nl + 1, $nonce, 0, $nonce.Length)

        $backend = [System.Net.Sockets.TcpClient]::new()
        $backend.NoDelay = $true
        $backend.Connect([System.Net.IPAddress]::Loopback, $port)
        $bs = $backend.GetStream()
        $bs.Write($nonce, 0, $nonce.Length)
        $bs.Flush()
    } catch {
        [Console]::Error.WriteLine("$Label relay: backend connect failed: $_")
        $client.Close()
        continue
    }

    $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace(
        [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault())
    $rs.Open()
    $ps = [System.Management.Automation.PowerShell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($HandlerSource).AddArgument(@($client, $bs, $backend))
    [void]$ps.BeginInvoke()
    $handlers += @{ PS = $ps; RS = $rs }

    # Reap finished connection handlers.
    $alive = @()
    foreach ($h in $handlers) {
        if ($h.PS.InvocationStateInfo.State -eq 'Running') {
            $alive += $h
        } else {
            try { $h.PS.EndInvoke($null) } catch {}
            $h.PS.Dispose(); $h.RS.Close(); $h.RS.Dispose()
        }
    }
    $handlers = $alive
}
'@

function New-RelayRunspace {
    # Start one relay accept loop in its own runspace. Returns a state
    # object for the watchdog loop.
    param(
        [Parameter(Mandatory)] [string]$Label,
        [Parameter(Mandatory)] [string]$RelaySource,
        [Parameter(Mandatory)] [object[]]$Arguments
    )

    $rs = [System.Management.Automation.Runspaces.RunspaceFactory]::CreateRunspace(
        [System.Management.Automation.Runspaces.InitialSessionState]::CreateDefault())
    $rs.Open()
    $ps = [System.Management.Automation.PowerShell]::Create()
    $ps.Runspace = $rs
    [void]$ps.AddScript($RelaySource)
    foreach ($arg in $Arguments) { [void]$ps.AddArgument($arg) }
    [void]$ps.BeginInvoke()
    return @{ Label = $Label; Runspace = $rs; PowerShell = $ps }
}

# --- Main -----------------------------------------------------------------

# Socket files do not need to exist yet: scdaemon in particular starts
# lazily on first card access, and the relays re-read the socket file for
# every connection.
$gpgconfDirs = Get-GpgconfDirs
$gpgSocket = $gpgconfDirs['agent-socket']
if (-not $gpgSocket) {
    $gpgSocket = Join-Path $gpgconfDirs['socketdir'] 'S.gpg-agent'
}
# gpgconf has no scdaemon-socket entry; it lives in the socket dir.
$scdaemonSocket = Join-Path $gpgconfDirs['socketdir'] 'S.scdaemon'

# All relays bind unconditionally. If a backend socket file does not exist
# yet (scdaemon starts lazily), connections fail with a logged error until
# the daemon appears.
$relays = @()
$relays += New-RelayRunspace -Label 'SSH agent' -RelaySource $PipeRelaySource `
    -Arguments @($SshPort, 'openssh-ssh-agent', 'SSH agent', $ConnectionHandlerSource)
$relays += New-RelayRunspace -Label 'GPG agent' -RelaySource $GpgRelaySource `
    -Arguments @($GpgPort, $gpgSocket, 'GPG agent', $ConnectionHandlerSource)
$relays += New-RelayRunspace -Label 'scdaemon' -RelaySource $GpgRelaySource `
    -Arguments @($ScdaemonPort, $scdaemonSocket, 'scdaemon', $ConnectionHandlerSource)

Write-Log ''
Write-Log 'Container can connect to:'
Write-Log "  SSH agent: host.docker.internal:$SshPort"
Write-Log "  GPG agent: host.docker.internal:$GpgPort"
Write-Log "  scdaemon:  host.docker.internal:$ScdaemonPort"
Write-Log 'Press Ctrl+C to stop.'

# Watchdog: a dead relay means a fatal setup error (e.g. port in use).
# Exit with an error so a supervising Scheduled Task can restart us.
while ($true) {
    Start-Sleep -Seconds 5
    foreach ($relay in $relays) {
        $state = $relay.PowerShell.InvocationStateInfo.State
        if ($state -ne 'Running') {
            $reason = $relay.PowerShell.InvocationStateInfo.Reason
            Write-Log "FATAL: $($relay.Label) relay stopped ($state): $reason"
            exit 1
        }
    }
}
