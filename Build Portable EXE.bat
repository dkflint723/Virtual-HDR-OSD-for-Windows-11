@echo off
setlocal EnableExtensions
cd /d "%~dp0"

echo.
echo Virtual HDR OSD for Windows - Portable EXE Builder
echo.
echo Output:
echo   release\Virtual HDR OSD for Windows.exe
echo.

set "NEEDS_RUNTIME=0"
if not exist ".venv\Scripts\python.exe" set "NEEDS_RUNTIME=1"
if "%NEEDS_RUNTIME%"=="0" ".venv\Scripts\python.exe" --version >nul 2>&1
if not "%NEEDS_RUNTIME%"=="1" if errorlevel 1 set "NEEDS_RUNTIME=1"
if "%NEEDS_RUNTIME%"=="1" (
    echo Preparing the project-local runtime first...
    if exist ".venv" rmdir /s /q ".venv"
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1"
    if errorlevel 1 goto :fail
)

if not exist ".uv\uv.exe" (
    echo ERROR: Project-local uv.exe was not found.
    echo Run "1- Install ^& Run.bat" once, then retry this builder.
    goto :fail
)

if exist ".build-venv" rmdir /s /q ".build-venv"
if exist "build-portable" rmdir /s /q "build-portable"
if exist "release\Virtual HDR OSD for Windows.exe" del /q "release\Virtual HDR OSD for Windows.exe"
if not exist "release" mkdir "release"

echo Validating the embedded watchdog resources...
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$installer=Join-Path '%~dp0' 'src\sdr_hdr_profile_creator\resources\2- OPTIONAL - Install-Watchdog.bat'; $uninstaller=Join-Path '%~dp0' 'src\sdr_hdr_profile_creator\resources\Uninstall-Watchdog.bat'; $i=Get-Content -Raw -LiteralPath $installer; $u=Get-Content -Raw -LiteralPath $uninstaller; if($i -notmatch 'Register-ScheduledTask' -or $i.Contains('CurrentVersion\Run') -or $u.Contains('CurrentVersion\Run')){ throw 'Embedded watchdog resources are stale. Expected Task Scheduler registration and no legacy Run-key registration.' }"
if errorlevel 1 goto :fail

set "UV_NO_CACHE=1"
set "UV_LINK_MODE=copy"
set "UV_PYTHON_INSTALL_DIR=%~dp0.python"
set "UV_PYTHON_BIN_DIR=%~dp0.python\bin"
set "UV_MANAGED_PYTHON=1"
set "UV_PYTHON_INSTALL_REGISTRY=0"

".uv\uv.exe" venv ".build-venv" --python ".venv\Scripts\python.exe"
if errorlevel 1 goto :fail

".uv\uv.exe" pip install --python ".build-venv\Scripts\python.exe" --no-cache "nuitka==4.1.3" "ordered-set" "zstandard"
if errorlevel 1 goto :fail

".uv\uv.exe" pip install --python ".build-venv\Scripts\python.exe" --no-cache -e .
if errorlevel 1 goto :fail

echo Running tests...
set "PYTHONPATH=%~dp0src"
".build-venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto :fail

echo.
echo Compiling one-file portable EXE...
".build-venv\Scripts\python.exe" -m nuitka ^
  --onefile ^
  --enable-plugin=pyside6 ^
  --windows-console-mode=disable ^
  --assume-yes-for-downloads ^
  --include-package=qfluentwidgets ^
  --include-package-data=qfluentwidgets ^
  --include-data-dir="src\sdr_hdr_profile_creator\resources=sdr_hdr_profile_creator/resources" ^
  --output-dir="build-portable" ^
  --output-filename="Virtual HDR OSD for Windows.exe" ^
  "tools\portable_entry.py"
if errorlevel 1 goto :fail

if not exist "build-portable\Virtual HDR OSD for Windows.exe" (
    echo ERROR: Nuitka completed without producing the expected EXE.
    goto :fail
)

copy /y "build-portable\Virtual HDR OSD for Windows.exe" "release\Virtual HDR OSD for Windows.exe" >nul
powershell.exe -NoProfile -Command "$p='%~dp0release\Virtual HDR OSD for Windows.exe'; $h=(Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant(); Set-Content -Encoding ASCII -LiteralPath '%~dp0release\Virtual HDR OSD for Windows.sha256.txt' -Value ($h + '  Virtual HDR OSD for Windows.exe')"

echo.
echo BUILD COMPLETE
echo.
echo   %~dp0release\Virtual HDR OSD for Windows.exe
echo.
echo The EXE is portable and does not require Python, uv, or the source tree on the destination PC.
echo The optional watchdog, when installed from Watchdog Settings, is intentionally deployed to LocalAppData so it can persist after the portable EXE closes or moves.
echo.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED. Review the error above.
pause
exit /b 1
