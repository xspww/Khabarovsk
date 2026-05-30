[CmdletBinding()]
param(
    [string]$ProjectRoot = ""
)

Set-StrictMode -Version 3.0
$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $ProjectRoot) {
    $ProjectRoot = (Resolve-Path (Join-Path $ScriptRoot "..")).Path
}

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "main.py"))) {
    throw "ProjectRoot does not contain main.py: $ProjectRoot"
}

function Assert-InProject([string]$Path) {
    $full = [System.IO.Path]::GetFullPath($Path)
    if (-not $full.StartsWith($ProjectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean outside project root: $full"
    }
    return $full
}

$dirNames = @("__pycache__", ".pytest_cache", ".mypy_cache", "htmlcov", "build", "dist")
$filePatterns = @("*.pyc", "*.pyo", ".coverage")

$removedDirs = 0
$removedFiles = 0

$dirs = @(Get-ChildItem -LiteralPath $ProjectRoot -Recurse -Force -Directory | Where-Object { $dirNames -contains $_.Name } | Sort-Object { $_.FullName.Length } -Descending)
foreach ($dir in $dirs) {
    if ($dirNames -contains $dir.Name) {
        $path = Assert-InProject $dir.FullName
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
            $removedDirs += 1
        }
    }
}

foreach ($pattern in $filePatterns) {
    foreach ($file in Get-ChildItem -LiteralPath $ProjectRoot -Recurse -Force -File -Filter $pattern) {
        Remove-Item -LiteralPath (Assert-InProject $file.FullName) -Force
        $removedFiles += 1
    }
}

[pscustomobject]@{
    ok = $true
    project_root = $ProjectRoot
    removed_dirs = $removedDirs
    removed_files = $removedFiles
} | ConvertTo-Json -Depth 3
