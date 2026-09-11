@echo off
setlocal EnableExtensions
title Color Profile Mode Watchdog - Uninstaller

echo.
echo Color Profile Mode Watchdog
echo Standalone uninstaller
echo.

rem Every step used to run under $ErrorActionPreference='SilentlyContinue' with the
rem task deletion in an empty catch{} and a hardcoded "exit 0", so the errorlevel test
rem below could never fire and the script printed "removed successfully" no matter what
rem happened. On a machine where the task belonged to an elevated install, that meant:
rem the task survived, the app directory was deleted out from under it, and the task
rem went on launching wscript.exe against a Launcher.vbs that no longer existed --
rem silently, because //B suppresses the dialog. Now each removal is verified and the
rem result is written where the GUI can read it.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='SilentlyContinue';" ^
  "$app=Join-Path $env:LOCALAPPDATA 'ColorProfileModeWatchdog';" ^
  "$watchdog=Join-Path $app 'Watchdog.ps1';" ^
  "$result=Join-Path $app 'install_result.json';" ^
  "$task='Virtual HDR OSD - Color Profile Mode Watchdog';" ^
  "$problems=@();" ^
  "try{ $svc=New-Object -ComObject 'Schedule.Service'; $svc.Connect(); $svc.GetFolder('\').DeleteTask($task,0) }catch{};" ^
  "try{ $svc2=New-Object -ComObject 'Schedule.Service'; $svc2.Connect(); $svc2.GetFolder('\').GetTask($task) | Out-Null; $problems += 'The scheduled task could not be removed: it was created by an elevated install, so this account cannot delete it. Re-run this uninstaller as administrator.' }catch{};" ^
  "Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Force;" ^
  "if (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -ErrorAction SilentlyContinue) { $problems += 'The startup registry entry could not be removed.' };" ^
  "$escaped=[Regex]::Escape($watchdog);" ^
  "Get-CimInstance Win32_Process | Where-Object { ($_.Name -ieq 'wscript.exe' -or $_.Name -ieq 'cscript.exe') -and $_.CommandLine -and ($_.CommandLine -like '*ColorProfileModeWatchdog*Launcher.vbs*') } | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null };" ^
  "Get-CimInstance Win32_Process | Where-Object { ($_.Name -ieq 'powershell.exe' -or $_.Name -ieq 'pwsh.exe') -and $_.CommandLine -and ($_.CommandLine -match $escaped) } | ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null };" ^
  "Start-Sleep -Milliseconds 300;" ^
  "$keep = $problems.Count -gt 0;" ^
  "if (-not $keep) { Remove-Item -LiteralPath $app -Recurse -Force } else { $problems += 'The watchdog files were left in place, because deleting them while the task survives would leave it launching a script that no longer exists.' };" ^
  "$startup=[Environment]::GetFolderPath('Startup');" ^
  "Remove-Item -LiteralPath (Join-Path $startup 'Color Profile Mode Watchdog.lnk') -Force;" ^
  "if ($keep) { try{ New-Item -ItemType Directory -Path $app -Force | Out-Null; [PSCustomObject]@{ action='uninstall'; ok=$false; warnings=$problems; at=(Get-Date).ToString('o') } | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $result -Encoding UTF8 }catch{} };" ^
  "foreach ($p in $problems) { Write-Warning $p };" ^
  "if ($keep) { exit 1 } else { exit 0 }"

if errorlevel 1 (
    echo.
    echo Uninstall did not fully complete - see the warnings above.
    pause
    exit /b 1
)

echo Watchdog removed successfully.
echo No color profiles were deleted or modified by the uninstaller.
echo.
pause
exit /b 0
