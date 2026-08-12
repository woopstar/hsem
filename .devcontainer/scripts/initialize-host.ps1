#requires -Version 5.1
<#
.SYNOPSIS
    Stages host SSH config and gitconfig for devcontainer bind mounts.

.DESCRIPTION
    Devcontainer variable substitution cannot reliably resolve the correct
    host path on Windows. This script runs on the Windows host before the
    container starts, snapshots %USERPROFILE%\.ssh and %USERPROFILE%\.gitconfig
    into a known workspace-relative path, and ensures the mount sources always
    exist.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$StageDir = Join-Path (Join-Path $RepoRoot '.devcontainer') 'host-config'

New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

$HostConfigDir = $env:USERPROFILE
if (-not $HostConfigDir) {
    Write-Error 'Unable to determine host config directory (USERPROFILE not set).'
}

Write-Host "Staging host config from: $HostConfigDir"

$SshSource = Join-Path $HostConfigDir '.ssh'
$SshTarget = Join-Path $StageDir '.ssh'
if (Test-Path $SshSource) {
    if (Test-Path $SshTarget) {
        Remove-Item -Recurse -Force $SshTarget
    }
    Copy-Item -Recurse -Path $SshSource -Destination $SshTarget
} else {
    New-Item -ItemType Directory -Force -Path $SshTarget | Out-Null
}

$GitconfigSource = Join-Path $HostConfigDir '.gitconfig'
$GitconfigTarget = Join-Path $StageDir '.gitconfig'
if (Test-Path $GitconfigSource) {
    $GitconfigItem = Get-Item -LiteralPath $GitconfigSource
    if ($GitconfigItem.PSIsContainer) {
        New-Item -ItemType File -Force -Path $GitconfigTarget | Out-Null
    } else {
        Copy-Item -LiteralPath $GitconfigSource -Destination $GitconfigTarget -Force
    }
} else {
    New-Item -ItemType File -Force -Path $GitconfigTarget | Out-Null
}

Write-Host "Host config staged in: $StageDir"
