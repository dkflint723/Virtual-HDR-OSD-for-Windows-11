@echo off
setlocal
cd /d "%~dp0"

set "NEEDS_INSTALL=0"
if not exist ".venv\Scripts\pythonw.exe" set "NEEDS_INSTALL=1"
if "%NEEDS_INSTALL%"=="0" (
    ".venv\Scripts\python.exe" -c "import PySide6, qfluentwidgets" >nul 2>&1
    if errorlevel 1 set "NEEDS_INSTALL=1"
)

if "%NEEDS_INSTALL%"=="1" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install.ps1"
    if errorlevel 1 (
        echo.
        echo Installation failed. Review the message above.
        pause
        exit /b 1
    )
)

start "Virtual HDR OSD for Windows" "%~dp0.venv\Scripts\pythonw.exe" -m sdr_hdr_profile_creator
endlocal
