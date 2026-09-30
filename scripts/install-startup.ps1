# Run this once in an Administrator PowerShell after Jev Remote works manually.
# It creates a sign-in task and a Private-network firewall rule for TCP 8787.
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$StartScript = Join-Path $PSScriptRoot "start.ps1"
$TaskName = "Jev Remote"

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`"" `
    -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "LAN-only Jev phone remote" -Force

if (-not (Get-NetFirewallRule -DisplayName "Jev Remote (Private LAN)" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule `
        -DisplayName "Jev Remote (Private LAN)" `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort 8787 `
        -Profile Private
}

Write-Host "Installed '$TaskName' and the Private-network firewall rule."
Write-Host "The task starts when $env:USERNAME signs in."
