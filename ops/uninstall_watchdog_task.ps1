[CmdletBinding()]
param(
    [string]$TaskName = "Cronus Launcher Watchdog"
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    [pscustomobject]@{ TaskName = $TaskName; Removed = $true }
}
else {
    [pscustomobject]@{ TaskName = $TaskName; Removed = $false }
}
