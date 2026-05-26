[CmdletBinding()]
param(
    [string]$TaskName = "Cronus Launcher Watchdog",
    [string]$LogDir = "",
    [int]$Port = 7777
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

if (-not $LogDir) {
    $LogDir = Join-Path (Join-Path $env:LOCALAPPDATA "Cronus Launcher\data") "logs"
}
$WatchdogLog = Join-Path $LogDir "cronus_watchdog.log"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskInfo = if ($task) { Get-ScheduledTaskInfo -TaskName $TaskName } else { $null }
$ExpectedProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$taskAction = $null
if ($task) {
    $actions = @($task.Actions)
    if ($actions.Count -gt 0) {
        $taskAction = $actions[0]
    }
}
$TaskArguments = if ($taskAction) { [string]$taskAction.Arguments } else { "" }
$TaskWorkingDirectory = if ($taskAction) { [string]$taskAction.WorkingDirectory } else { "" }
$WatchdogScript = Join-Path $ExpectedProjectRoot "ops\cronus_watchdog.ps1"
$TaskWorkingDirectoryMatches = $false
$TaskArgumentsContainExpectedProjectRoot = $false
if ($TaskWorkingDirectory) {
    try {
        $actualRoot = [System.IO.Path]::GetFullPath($TaskWorkingDirectory).TrimEnd('\')
        $expectedRoot = [System.IO.Path]::GetFullPath($ExpectedProjectRoot).TrimEnd('\')
        $TaskWorkingDirectoryMatches = ($actualRoot -ieq $expectedRoot)
    }
    catch {
        $TaskWorkingDirectoryMatches = $false
    }
}
if ($TaskArguments) {
    $TaskArgumentsContainExpectedProjectRoot = $TaskArguments.ToLowerInvariant().Contains($ExpectedProjectRoot.ToLowerInvariant())
}
$ProjectRootMatches = [bool]($TaskWorkingDirectoryMatches -and $TaskArgumentsContainExpectedProjectRoot)
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
    ExpectedProjectRoot = $ExpectedProjectRoot
    TaskWorkingDirectory = $TaskWorkingDirectory
    TaskArguments = $TaskArguments
    TaskWorkingDirectoryMatches = [bool]$TaskWorkingDirectoryMatches
    TaskArgumentsContainExpectedProjectRoot = [bool]$TaskArgumentsContainExpectedProjectRoot
    ProjectRootMatches = [bool]$ProjectRootMatches
    WatchdogScriptExists = (Test-Path -LiteralPath $WatchdogScript)
    BackendHealthy = [bool]$health
    StatusRevision = if ($health) { $health.status_revision } else { $null }
    WatchdogLog = $WatchdogLog
    RecentLog = if (Test-Path -LiteralPath $WatchdogLog) { @(Get-Content -LiteralPath $WatchdogLog -Tail 8 | ForEach-Object { [string]$_ }) } else { @() }
}
