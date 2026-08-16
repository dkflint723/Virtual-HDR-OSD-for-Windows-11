$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$UvDir = Join-Path $Root ".uv"
$UvExe = Join-Path $UvDir "uv.exe"
$PythonDir = Join-Path $Root ".python"
$VenvDir = Join-Path $Root ".venv"

# Pin the bootstrap version instead of downloading an unbounded "latest" build.
# This keeps installs reproducible and avoids unexpected antivirus regressions.
$UvVersion = "0.11.32"
$UvInstallerUrl = "https://astral.sh/uv/$UvVersion/install.ps1"

$env:UV_UNMANAGED_INSTALL = $UvDir
$env:UV_PYTHON_INSTALL_DIR = $PythonDir
$env:UV_PYTHON_BIN_DIR = Join-Path $PythonDir "bin"
$env:UV_NO_CACHE = "1"
$env:UV_LINK_MODE = "copy"
$env:UV_PROJECT_ENVIRONMENT = $VenvDir
$env:UV_MANAGED_PYTHON = "1"
$env:UV_PYTHON_INSTALL_REGISTRY = "0"
$env:UV_NO_MODIFY_PATH = "1"

Write-Host "Virtual HDR OSD for Windows - local installation" -ForegroundColor Cyan
Write-Host "Project: $Root"
Write-Host "Bootstrap uv: $UvVersion"

$UvRunner = $null

if (Test-Path $UvExe) {
    $UvRunner = $UvExe
}
else {
    Write-Host "Downloading project-local uv $UvVersion..."
    try {
        # Use the official, versioned Astral installer. Do not use an unpinned latest URL.
        $installer = Invoke-RestMethod -Uri $UvInstallerUrl
        Invoke-Expression $installer
    }
    catch {
        Write-Warning "The project-local uv bootstrap was blocked or failed: $($_.Exception.Message)"
    }

    if (Test-Path $UvExe) {
        $UvRunner = $UvExe
    }
}

# If Defender quarantines the local executable but the user already has uv installed,
# use that existing executable only for bootstrap. Python and .venv remain project-local.
if (-not $UvRunner) {
    $systemUv = Get-Command uv.exe -ErrorAction SilentlyContinue
    if ($systemUv) {
        $UvRunner = $systemUv.Source
        Write-Host "Using existing uv only for bootstrap: $UvRunner" -ForegroundColor Yellow
    }
}

if (-not $UvRunner) {
    Write-Host "" 
    Write-Host "Microsoft Defender appears to have blocked the official uv executable." -ForegroundColor Red
    Write-Host "The app itself has not been installed or executed yet." -ForegroundColor Red
    Write-Host "Do not disable Windows Security for this app." -ForegroundColor Yellow
    Write-Host "Update Defender security intelligence, then retry `"1- Install & Run.bat`"." -ForegroundColor Yellow
    Write-Host "You can also install uv from Astral separately and rerun this installer; it will use it only for bootstrap." -ForegroundColor Yellow
    throw "No usable uv executable is available."
}

Write-Host "Installing project-local Python 3.12..."
& $UvRunner python install 3.12 --no-bin --no-registry
if ($LASTEXITCODE -ne 0) { throw "uv python install failed with exit code $LASTEXITCODE" }

Write-Host "Synchronizing the project-local environment..."
& $UvRunner sync --no-cache --python 3.12
if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }

Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Python: $PythonDir"
Write-Host "Environment: $VenvDir"
Write-Host "Start the app with `"1- Install & Run.bat`""
