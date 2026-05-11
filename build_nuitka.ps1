param(
    [switch]$SkipTests,
    [switch]$Clean,
    [switch]$NoMingwFallback
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildRoot = Join-Path $ProjectRoot "build"
$VenvRoot = Join-Path $BuildRoot "nuitka-venv"
$DistRoot = Join-Path $ProjectRoot "dist"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$OutputExe = Join-Path $DistRoot "ArgusLauncher.exe"
$IconPath = Join-Path $ProjectRoot "assets\argus_launcher.ico"

function Write-Step {
    param([string]$Message)
    Write-Host "[build] $Message" -ForegroundColor Cyan
}

function Require-File {
    param([string]$Path, [string]$Label)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
}

function Test-Command {
    param([string]$Name)
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    return $null -ne $cmd
}

function Invoke-Checked {
    param(
        [string]$Label,
        [string]$FilePath,
        [string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

Push-Location $ProjectRoot
try {
    Require-File (Join-Path $ProjectRoot "main.py") "Entrypoint"
    Require-File (Join-Path $ProjectRoot "requirements-build.txt") "Build requirements"
    Require-File $IconPath "Application icon"

    if ($Clean) {
        Write-Step "Cleaning previous Nuitka output"
        Remove-Item -LiteralPath (Join-Path $BuildRoot "nuitka-output") -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $OutputExe -Force -ErrorAction SilentlyContinue
    }

    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        Write-Step "Creating Python 3.11 build venv"
        Invoke-Checked "Create build venv" "py" @("-3.11", "-m", "venv", $VenvRoot)
    }

    Write-Step "Installing build and runtime dependencies"
    Invoke-Checked "Upgrade pip" $Python @("-m", "pip", "install", "--upgrade", "pip")
    Invoke-Checked "Install requirements" $Python @("-m", "pip", "install", "-r", (Join-Path $ProjectRoot "requirements-build.txt"))

    if (-not $SkipTests) {
        Write-Step "Running compile check"
        Invoke-Checked "Compile check" $Python @("-m", "compileall", "-q", ".")

        Write-Step "Running unit tests"
        Invoke-Checked "Unit tests" $Python @("-m", "unittest", "discover", "-s", "tests", "-v")
    }

    New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null

    $compilerArgs = @()
    if (Test-Command "cl.exe") {
        Write-Step "Using MSVC compiler"
        $compilerArgs += "--msvc=latest"
    } elseif (-not $NoMingwFallback) {
        Write-Step "MSVC not found; using Nuitka MinGW64 compiler download"
        $compilerArgs += "--mingw64"
        $compilerArgs += "--assume-yes-for-downloads"
    } else {
        throw "No C compiler found. Install Visual Studio 2022 Build Tools with C++ workload, or rerun without -NoMingwFallback to let Nuitka use MinGW64."
    }

    $nuitkaArgs = @(
        "--mode=onefile",
        "--remove-output",
        "--output-dir=$DistRoot",
        "--output-filename=ArgusLauncher.exe",
        "--windows-console-mode=disable",
        "--windows-icon-from-ico=$IconPath",
        "--product-name=Argus Launcher",
        "--file-description=Argus Launcher",
        "--company-name=Argus Launcher",
        "--product-version=1.0.0.0",
        "--file-version=1.0.0.0",
        "--copyright=Copyright (c) 2026",
        "--enable-plugin=pyside6",
        "--include-package=fastapi",
        "--include-package=starlette",
        "--include-package=pydantic",
        "--include-package=uvicorn",
        "--include-module=multi_roblox_guard",
        "--include-data-dir=assets=assets",
        "--include-data-dir=vision_templates=vision_templates",
        "--python-flag=no_asserts",
        "--python-flag=no_docstrings",
        "--python-flag=isolated",
        "--python-flag=safe_path",
        "--report=build\nuitka-report.xml",
        "main.py"
    )

    Write-Step "Building single-file executable with Nuitka"
    Invoke-Checked "Nuitka build" $Python (@("-m", "nuitka") + $compilerArgs + $nuitkaArgs)

    if (-not (Test-Path -LiteralPath $OutputExe -PathType Leaf)) {
        throw "Build finished without expected output: $OutputExe"
    }

    $sizeMb = [math]::Round((Get-Item -LiteralPath $OutputExe).Length / 1MB, 2)
    Write-Step "Created $OutputExe ($sizeMb MB)"
} finally {
    Pop-Location
}
