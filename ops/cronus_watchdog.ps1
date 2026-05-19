[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$Python = "python",
    [string]$HostName = "127.0.0.1",
    [int]$Port = 7777,
    [int]$IntervalSeconds = 15,
    [int]$FailureThreshold = 3,
    [int]$StartupGraceSeconds = 45,
    [int]$RestartBackoffSeconds = 20,
    [string]$LogDir = "",
    [switch]$Once,
    [switch]$NoStart
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
}

function Resolve-FullPath([string]$Path) {
    return [System.IO.Path]::GetFullPath($Path)
}

function Quote-Arg([string]$Value) {
    return '"' + ($Value -replace '"', '\"') + '"'
}

$ProjectRoot = Resolve-FullPath $ProjectRoot
$Runner = Join-Path $ProjectRoot "ops\run_backend.py"
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "main.py"))) {
    throw "ProjectRoot does not contain main.py: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath $Runner)) {
    throw "Backend runner not found: $Runner"
}

if (-not $LogDir) {
    $LogDir = Join-Path $env:LOCALAPPDATA "Cronus Launcher\data"
}
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$WatchdogLog = Join-Path $LogDir "cronus_watchdog.log"
$HealthUrl = "http://$HostName`:$Port/api/status"
$RunnerMatch = (Resolve-FullPath $Runner).ToLowerInvariant()

function Write-WatchdogLog([string]$Level, [string]$Message) {
    $stamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.fffK")
    Add-Content -LiteralPath $WatchdogLog -Encoding UTF8 -Value "$stamp [$Level] $Message"
}

function Test-CronusHealth {
    try {
        $response = Invoke-RestMethod -Uri $HealthUrl -Method Get -TimeoutSec 3
        return [pscustomobject]@{
            Ok = $true
            Detail = "status ok revision=$($response.status_revision)"
        }
    }
    catch {
        return [pscustomobject]@{
            Ok = $false
            Detail = $_.Exception.Message
        }
    }
}

function Get-CronusBackendProcesses {
    Get-CimInstance Win32_Process |
        Where-Object {
            $cmd = [string]($_.CommandLine)
            $cmd -and $cmd.ToLowerInvariant().Contains($RunnerMatch)
        }
}

function Stop-KnownCronusBackends([string]$Reason) {
    $stopped = 0
    foreach ($proc in @(Get-CronusBackendProcesses)) {
        try {
            Stop-Process -Id ([int]$proc.ProcessId) -Force -ErrorAction Stop
            $stopped += 1
            Write-WatchdogLog "WARN" "stopped known backend pid=$($proc.ProcessId) reason=$Reason"
        }
        catch {
            Write-WatchdogLog "ERROR" "failed to stop known backend pid=$($proc.ProcessId): $($_.Exception.Message)"
        }
    }
    return $stopped
}

function Start-CronusBackend {
    $runId = Get-Date -Format "yyyyMMdd-HHmmss"
    $stdout = Join-Path $LogDir "cronus_backend_$runId.out.log"
    $stderr = Join-Path $LogDir "cronus_backend_$runId.err.log"
    $argLine = @(
        "-u",
        (Quote-Arg $Runner),
        "--host",
        $HostName,
        "--port",
        [string]$Port,
        "--log-level",
        "warning"
    ) -join " "
    $env:PYTHONUTF8 = "1"
    $env:PYTHONIOENCODING = "utf-8"
    $process = Start-Process `
        -FilePath $Python `
        -ArgumentList $argLine `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -PassThru
    Write-WatchdogLog "INFO" "started backend pid=$($process.Id) port=$Port stdout=$stdout stderr=$stderr"
    return $process
}

function Wait-CronusReady([int]$TimeoutSeconds) {
    $deadline = (Get-Date).AddSeconds([Math]::Max(1, $TimeoutSeconds))
    do {
        $health = Test-CronusHealth
        if ($health.Ok) {
            return $true
        }
        Start-Sleep -Milliseconds 750
    } while ((Get-Date) -lt $deadline)
    return $false
}

$mutex = New-Object System.Threading.Mutex($false, "Global\CronusLauncherWatchdog")
if (-not $mutex.WaitOne(0)) {
    Write-WatchdogLog "INFO" "another watchdog instance is already running"
    exit 0
}

try {
    $IntervalSeconds = [Math]::Max(5, $IntervalSeconds)
    $FailureThreshold = [Math]::Max(1, $FailureThreshold)
    $StartupGraceSeconds = [Math]::Max(5, $StartupGraceSeconds)
    $RestartBackoffSeconds = [Math]::Max(5, $RestartBackoffSeconds)
    $failures = 0
    $lastRestartAt = [DateTime]::MinValue
    Write-WatchdogLog "INFO" "watchdog started root=$ProjectRoot port=$Port interval=$IntervalSeconds threshold=$FailureThreshold"

    while ($true) {
        $health = Test-CronusHealth
        if ($health.Ok) {
            $failures = 0
            if ($Once) {
                Write-WatchdogLog "INFO" "health ok once: $($health.Detail)"
            }
        }
        else {
            $failures += 1
            Write-WatchdogLog "WARN" "health failure count=$failures detail=$($health.Detail)"
            if ($failures -ge $FailureThreshold) {
                $sinceRestart = ((Get-Date) - $lastRestartAt).TotalSeconds
                if ($sinceRestart -lt $RestartBackoffSeconds) {
                    Write-WatchdogLog "WARN" "restart suppressed by backoff remaining=$([Math]::Round($RestartBackoffSeconds - $sinceRestart, 1))s"
                }
                elseif ($NoStart) {
                    Write-WatchdogLog "WARN" "restart skipped because -NoStart is set"
                }
                else {
                    Stop-KnownCronusBackends "health_failed" | Out-Null
                    Start-CronusBackend | Out-Null
                    $lastRestartAt = Get-Date
                    $failures = 0
                    if (-not (Wait-CronusReady $StartupGraceSeconds)) {
                        Write-WatchdogLog "ERROR" "backend did not become healthy within $StartupGraceSeconds seconds"
                    }
                    else {
                        Write-WatchdogLog "INFO" "backend healthy after restart"
                    }
                }
            }
        }

        if ($Once) {
            break
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
}
finally {
    try {
        $mutex.ReleaseMutex() | Out-Null
        $mutex.Dispose()
    }
    catch {
    }
}
