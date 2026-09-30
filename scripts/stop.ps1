# Stops an auto-started Jev Remote task. For a manually started server, press Ctrl+C
# in its PowerShell window instead.
$ErrorActionPreference = "Stop"
$TaskName = "Jev Remote"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName
    Write-Host "Jev Remote scheduled task stopped."
} else {
    Write-Host "No Jev Remote scheduled task is installed."
    Write-Host "If the server is running manually, press Ctrl+C in its PowerShell window."
}
