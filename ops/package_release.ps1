[CmdletBinding()]
param(
    [string]$ProjectRoot = "",
    [string]$OutputDir = ""
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
}
if (-not $OutputDir) {
    $OutputDir = Join-Path $ProjectRoot "dist"
}

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "main.py"))) {
    throw "ProjectRoot does not contain main.py: $ProjectRoot"
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$stage = Join-Path ([System.IO.Path]::GetTempPath()) "cronus-release-$stamp"
$zipPath = Join-Path $OutputDir "CronusLauncher-$stamp.zip"

$excludedDirs = @(
    ".git",
    ".pytest_cache",
    "__pycache__",
    "build",
    "dist",
    "data",
    "logs",
    "cronus_rt1_instances"
)

$excludedFiles = @(
    "AccountData.json",
    "account_import_pending.json",
    "account_tools_audit.jsonl",
    "cronus_rt1.log",
    "cronus_rt1_events.jsonl",
    "cronus_rt1_cookies.json",
    "cronus_rt12_runtime.txt",
    "cronus_runtime.db",
    "cronus_runtime.db-shm",
    "cronus_runtime.db-wal",
    "cronus_rt_instance.json",
    "cronus_watchdog.log",
    "release_gate_last.json",
    "watchdog_task_last.json"
)

$excludedFilePatterns = @(
    "cronus_backend_*.log",
    "control_plane_soak_*.jsonl",
    "soak_monitor_*.jsonl"
)

function Test-ReleaseExcluded([System.IO.FileSystemInfo]$Item) {
    $relative = $Item.FullName.Substring($ProjectRoot.Length).TrimStart("\", "/")
    $parts = $relative -split '[\\/]'
    foreach ($part in $parts) {
        if ($excludedDirs -contains $part) {
            return $true
        }
    }
    if ($excludedFiles -contains $Item.Name) {
        return $true
    }
    foreach ($pattern in $excludedFilePatterns) {
        if ($Item.Name -like $pattern) {
            return $true
        }
    }
    if ($Item.Name -like "*.pyc" -or $Item.Name -like "*.pyo") {
        return $true
    }
    return $false
}

function Get-SourceCommit {
    try {
        $commit = (& git -C $ProjectRoot rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -eq 0 -and $commit) {
            return ([string]$commit).Trim()
        }
    }
    catch {
    }
    return ""
}

if (Test-Path -LiteralPath $stage) {
    $resolvedStage = [System.IO.Path]::GetFullPath($stage)
    if (-not $resolvedStage.StartsWith([System.IO.Path]::GetTempPath(), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected staging path: $resolvedStage"
    }
    Remove-Item -LiteralPath $resolvedStage -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

try {
    foreach ($item in Get-ChildItem -LiteralPath $ProjectRoot -Force -Recurse) {
        if (Test-ReleaseExcluded $item) {
            continue
        }
        $relative = $item.FullName.Substring($ProjectRoot.Length).TrimStart("\", "/")
        $target = Join-Path $stage $relative
        if ($item.PSIsContainer) {
            New-Item -ItemType Directory -Force -Path $target | Out-Null
        }
        else {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $item.FullName -Destination $target -Force
        }
    }

    $manifestPath = Join-Path $stage "release-manifest.json"
    [ordered]@{
        name = "Cronus Launcher"
        created_at = (Get-Date).ToUniversalTime().ToString("o")
        source_commit = Get-SourceCommit
        excluded_runtime_data = $true
        excluded_files = $excludedFiles
        excluded_file_patterns = $excludedFilePatterns
        excluded_dirs = $excludedDirs
    } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8

    if (Test-Path -LiteralPath $zipPath) {
        Remove-Item -LiteralPath $zipPath -Force
    }
    Compress-Archive -Path (Join-Path $stage "*") -DestinationPath $zipPath -Force
    [pscustomobject]@{
        ok = $true
        zip = $zipPath
        manifest = "release-manifest.json"
        excluded_files = $excludedFiles
        excluded_file_patterns = $excludedFilePatterns
        excluded_dirs = $excludedDirs
    } | ConvertTo-Json -Depth 4
}
finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
