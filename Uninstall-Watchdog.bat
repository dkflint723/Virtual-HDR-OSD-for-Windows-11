@echo off
setlocal EnableExtensions
title Color Profile Mode Watchdog - Uninstaller

echo.
echo Color Profile Mode Watchdog
echo Standalone uninstaller
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='SilentlyContinue';" ^
  "$app=Join-Path $env:LOCALAPPDATA 'ColorProfileModeWatchdog';" ^
  "$watchdog=Join-Path $app 'Watchdog.ps1';" ^
  "$task='Virtual HDR OSD - Color Profile Mode Watchdog';" ^
  "Unregister-ScheduledTask -TaskName $task -Confirm:`$false;" ^
  "$escaped=[Regex]::Escape($watchdog);" ^
  "Get-CimInstance Win32_Process | Where-Object { ($_.Name -ieq 'powershell.exe' -or $_.Name -ieq 'pwsh.exe') -and $_.CommandLine -and ($_.CommandLine -match $escaped) } | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null };" ^
  "Start-Sleep -Milliseconds 300;" ^
  "Remove-Item -LiteralPath $app -Recurse -Force;" ^
  "$startup=[Environment]::GetFolderPath('Startup');" ^
  "Remove-Item -LiteralPath (Join-Path $startup 'Color Profile Mode Watchdog.lnk') -Force;" ^
  "exit 0"

if errorlevel 1 (
    echo.
    echo Uninstall encountered an error.
    pause
    exit /b 1
)

echo Watchdog removed successfully.
echo No color profiles were deleted or modified by the uninstaller.
echo.
pause
exit /b 0
