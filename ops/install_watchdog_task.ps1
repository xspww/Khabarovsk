[CmdletBinding()]
param(
    [string]$TaskName = "Cronus Launcher Watchdog",
    [string]$ProjectRoot = "",
    [string]$Python = "python",
    [int]$Port = 7777,
    [int]$IntervalSeconds = 15,
    [int]$FailureThreshold = 3,
    [int]$StartupGraceSeconds = 45,
    [int]$RestartBackoffSeconds = 20
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$Watchdog = Join-Path $ProjectRoot "ops\cronus_watchdog.ps1"
if (-not (Test-Path -LiteralPath $Watchdog)) {
    throw "Watchdog script not found: $Watchdog"
}

$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$Argument = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$Watchdog`"",
    "-ProjectRoot", "`"$ProjectRoot`"",
    "-Python", "`"$Python`"",
    "-Port", [string]$Port,
    "-IntervalSeconds", [string]$IntervalSeconds,
    "-FailureThreshold", [string]$FailureThreshold,
    "-StartupGraceSeconds", [string]$StartupGraceSeconds,
    "-RestartBackoffSeconds", [string]$RestartBackoffSeconds
) -join " "

$Action = New-ScheduledTaskAction -Execute $PowerShellExe -Argument $Argument -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Keeps the Cronus Launcher backend available for local 24/7 Roblox farm monitoring." `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $TaskName
$info = Get-ScheduledTaskInfo -TaskName $TaskName
[pscustomobject]@{
    TaskName = $task.TaskName
    State = $task.State
    LastRunTime = $info.LastRunTime
    LastTaskResult = $info.LastTaskResult
    Watchdog = $Watchdog
    ProjectRoot = $ProjectRoot
}
