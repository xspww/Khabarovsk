[CmdletBinding()]
param(
    [string]$TaskName = "Cronus Launcher Watchdog",
    [string]$LogDir = "",
    [int]$Port = 7777
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

if (-not $LogDir) {
    $LogDir = Join-Path $env:LOCALAPPDATA "Cronus Launcher\data"
}
$WatchdogLog = Join-Path $LogDir "cronus_watchdog.log"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$health = $null
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/status" -TimeoutSec 3
}
catch {
}

[pscustomobject]@{
    TaskInstalled = [bool]$task
    TaskState = if ($task) { [string]$task.State } else { "" }
    LastRunTime = if ($taskInfo) { $taskInfo.LastRunTime } else { $null }
    LastTaskResult = if ($taskInfo) { $taskInfo.LastTaskResult } else { $null }
    BackendHealthy = [bool]$health
    StatusRevision = if ($health) { $health.status_revision } else { $null }
    WatchdogLog = $WatchdogLog
    RecentLog = if (Test-Path -LiteralPath $WatchdogLog) { Get-Content -LiteralPath $WatchdogLog -Tail 8 } else { @() }
}
