@echo off
setlocal EnableExtensions
title Color Profile Mode Watchdog - Installer

set "APPDIR=%LOCALAPPDATA%\ColorProfileModeWatchdog"
set "WATCHDOG=%APPDIR%\Watchdog.ps1"
set "INSTALL_BAT=%~f0"
set "WATCHDOG_PATH=%WATCHDOG%"

echo.
echo Color Profile Mode Watchdog
echo Standalone installer - no Python, uv, or external app required.
echo.

if not exist "%APPDIR%" mkdir "%APPDIR%" >nul 2>&1
if errorlevel 1 (
    echo ERROR: Could not create:
    echo   %APPDIR%
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$raw = Get-Content -Raw -LiteralPath $env:INSTALL_BAT; $marker=':__WATCHDOG_POWERSHELL_PAYLOAD__'; $i=$raw.LastIndexOf($marker); if($i -lt 0){throw 'Embedded watchdog payload was not found.'}; $payload=$raw.Substring($i+$marker.Length).TrimStart([char]13,[char]10); Set-Content -LiteralPath $env:WATCHDOG_PATH -Value $payload -Encoding UTF8" ^
  1>nul
if errorlevel 1 (
    echo ERROR: Could not extract the standalone watchdog.
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$p=$env:WATCHDOG_PATH; $raw=Get-Content -Raw -LiteralPath $p; if(-not $raw.TrimStart().StartsWith('param(')){ throw 'Extracted watchdog is invalid.' }; if($raw -notmatch '\$nativeSource\s*=\s*@'''){ throw 'Embedded native API block is missing.' }" ^
  1>nul
if errorlevel 1 (
    echo ERROR: The extracted watchdog failed integrity validation.
    del /q "%WATCHDOG%" >nul 2>&1
    pause
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%WATCHDOG%" -Install
if errorlevel 1 (
    echo.
    echo Installation failed. Review the message above.
    pause
    exit /b 1
)

echo.
echo Installation complete.
echo The watchdog now runs invisibly at sign-in.
echo.
echo IMPORTANT:
echo If you intentionally change your SDR or HDR default profiles later,
echo run this installer again to capture the new associations.
echo.
pause
exit /b 0

:__WATCHDOG_POWERSHELL_PAYLOAD__
param(
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$AppDir = Join-Path $env:LOCALAPPDATA 'ColorProfileModeWatchdog'
$StatePath = Join-Path $AppDir 'State.json'
$LogPath = Join-Path $AppDir 'Watchdog.log'
$LauncherPath = Join-Path $AppDir 'Launcher.vbs'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$RunName = 'ColorProfileModeWatchdog'

function Write-Log {
    param([string]$Message)

    try {
        if (Test-Path -LiteralPath $LogPath) {
            $item = Get-Item -LiteralPath $LogPath -ErrorAction SilentlyContinue
            if ($item -and $item.Length -gt 524288) {
                Move-Item -Force -LiteralPath $LogPath -Destination ($LogPath + '.old') -ErrorAction SilentlyContinue
            }
        }
        $line = '{0:yyyy-MM-dd HH:mm:ss.fff}  {1}' -f (Get-Date), $Message
        Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    } catch {}
}

function Stop-ExistingWatchdog {
    try {
        $escaped = [Regex]::Escape((Join-Path $AppDir 'Watchdog.ps1'))
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -ieq 'powershell.exe' -or $_.Name -ieq 'pwsh.exe') -and
                $_.CommandLine -and
                ($_.CommandLine -match $escaped) -and
                ($_.ProcessId -ne $PID)
            } |
            ForEach-Object {
                Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null
            }
    } catch {}
}

$nativeSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

namespace ColorProfileWatchdog
{
    public static class Native
    {
        public const uint QDC_ONLY_ACTIVE_PATHS = 0x00000002;
        public const int ERROR_SUCCESS = 0;
        public const int ERROR_INSUFFICIENT_BUFFER = 122;

        public const int WCS_PROFILE_MANAGEMENT_SCOPE_SYSTEM_WIDE = 0;
        public const int WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER = 1;
        public const int CPT_ICC = 0;
        public const int CPST_STANDARD_DISPLAY_COLOR_MODE = 7;
        public const int CPST_EXTENDED_DISPLAY_COLOR_MODE = 8;

        public const int DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME = 1;
        public const int DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO = 9;

        [StructLayout(LayoutKind.Sequential)]
        public struct LUID
        {
            public UInt32 LowPart;
            public Int32 HighPart;

            public override string ToString()
            {
                return HighPart.ToString("X8") + LowPart.ToString("X8");
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_RATIONAL
        {
            public UInt32 Numerator;
            public UInt32 Denominator;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_PATH_SOURCE_INFO
        {
            public LUID adapterId;
            public UInt32 id;
            public UInt32 modeInfoIdx;
            public UInt32 statusFlags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_PATH_TARGET_INFO
        {
            public LUID adapterId;
            public UInt32 id;
            public UInt32 modeInfoIdx;
            public Int32 outputTechnology;
            public Int32 rotation;
            public Int32 scaling;
            public DISPLAYCONFIG_RATIONAL refreshRate;
            public Int32 scanLineOrdering;

            [MarshalAs(UnmanagedType.Bool)]
            public bool targetAvailable;

            public UInt32 statusFlags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_PATH_INFO
        {
            public DISPLAYCONFIG_PATH_SOURCE_INFO sourceInfo;
            public DISPLAYCONFIG_PATH_TARGET_INFO targetInfo;
            public UInt32 flags;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_DEVICE_INFO_HEADER
        {
            public Int32 type;
            public UInt32 size;
            public LUID adapterId;
            public UInt32 id;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        public struct DISPLAYCONFIG_SOURCE_DEVICE_NAME
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;

            [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)]
            public string viewGdiDeviceName;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
            public UInt32 value;
            public Int32 colorEncoding;
            public UInt32 bitsPerColorChannel;
        }

        public sealed class DisplayInfo
        {
            public string GdiName { get; set; }
            public UInt32 AdapterLow { get; set; }
            public Int32 AdapterHigh { get; set; }
            public UInt32 SourceId { get; set; }
            public UInt32 TargetId { get; set; }
            public bool AdvancedColorEnabled { get; set; }

            internal LUID AdapterLuid
            {
                get
                {
                    LUID value = new LUID();
                    value.LowPart = AdapterLow;
                    value.HighPart = AdapterHigh;
                    return value;
                }
            }
        }

        [DllImport("user32.dll")]
        private static extern int GetDisplayConfigBufferSizes(
            UInt32 flags,
            out UInt32 numPathArrayElements,
            out UInt32 numModeInfoArrayElements);

        [DllImport("user32.dll")]
        private static extern int QueryDisplayConfig(
            UInt32 flags,
            ref UInt32 numPathArrayElements,
            [Out] DISPLAYCONFIG_PATH_INFO[] pathArray,
            ref UInt32 numModeInfoArrayElements,
            IntPtr modeInfoArray,
            IntPtr currentTopologyId);

        [DllImport("user32.dll")]
        private static extern int DisplayConfigGetDeviceInfo(
            ref DISPLAYCONFIG_SOURCE_DEVICE_NAME requestPacket);

        [DllImport("user32.dll", EntryPoint = "DisplayConfigGetDeviceInfo")]
        private static extern int DisplayConfigGetAdvancedColorInfo(
            ref DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO requestPacket);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern int ColorProfileGetDisplayDefault(
            int scope,
            LUID targetAdapterID,
            UInt32 sourceID,
            int profileType,
            int profileSubType,
            out IntPtr profileName);

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern int ColorProfileSetDisplayDefaultAssociation(
            int scope,
            string profileName,
            int profileType,
            int profileSubType,
            LUID targetAdapterID,
            UInt32 sourceID);

        [DllImport("mscms.dll")]
        private static extern int ColorProfileGetDisplayUserScope(
            LUID targetAdapterID,
            UInt32 sourceID,
            out int scope);

        [DllImport("kernel32.dll")]
        private static extern IntPtr LocalFree(IntPtr hMem);

        private static string GetSourceName(DISPLAYCONFIG_PATH_SOURCE_INFO source)
        {
            DISPLAYCONFIG_SOURCE_DEVICE_NAME packet = new DISPLAYCONFIG_SOURCE_DEVICE_NAME();
            packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SOURCE_NAME;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_SOURCE_DEVICE_NAME));
            packet.header.adapterId = source.adapterId;
            packet.header.id = source.id;
            packet.viewGdiDeviceName = String.Empty;

            int rc = DisplayConfigGetDeviceInfo(ref packet);
            if (rc != ERROR_SUCCESS)
                return null;

            return packet.viewGdiDeviceName;
        }

        private static bool GetAdvancedColorEnabled(DISPLAYCONFIG_PATH_TARGET_INFO target)
        {
            DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO packet = new DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO();
            packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_ADVANCED_COLOR_INFO;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_GET_ADVANCED_COLOR_INFO));
            packet.header.adapterId = target.adapterId;
            packet.header.id = target.id;

            int rc = DisplayConfigGetAdvancedColorInfo(ref packet);
            if (rc != ERROR_SUCCESS)
                return false;

            return (packet.value & 0x2u) != 0;
        }

        public static DisplayInfo[] GetActiveDisplays()
        {
            for (int attempt = 0; attempt < 4; attempt++)
            {
                UInt32 pathCount;
                UInt32 modeCount;
                int rc = GetDisplayConfigBufferSizes(QDC_ONLY_ACTIVE_PATHS, out pathCount, out modeCount);
                if (rc != ERROR_SUCCESS)
                    throw new InvalidOperationException("GetDisplayConfigBufferSizes failed: " + rc);

                DISPLAYCONFIG_PATH_INFO[] paths = new DISPLAYCONFIG_PATH_INFO[pathCount];

                // We do not inspect mode information. Over-allocate each entry so Windows
                // can safely populate its native DISPLAYCONFIG_MODE_INFO array.
                long modeBytes = Math.Max(1L, (long)modeCount) * 256L;
                IntPtr modeBuffer = Marshal.AllocHGlobal(new IntPtr(modeBytes));

                try
                {
                    UInt32 pathCount2 = pathCount;
                    UInt32 modeCount2 = modeCount;

                    rc = QueryDisplayConfig(
                        QDC_ONLY_ACTIVE_PATHS,
                        ref pathCount2,
                        paths,
                        ref modeCount2,
                        modeBuffer,
                        IntPtr.Zero);

                    if (rc == ERROR_INSUFFICIENT_BUFFER)
                        continue;

                    if (rc != ERROR_SUCCESS)
                        throw new InvalidOperationException("QueryDisplayConfig failed: " + rc);

                    List<DisplayInfo> result = new List<DisplayInfo>();

                    for (int i = 0; i < pathCount2; i++)
                    {
                        DISPLAYCONFIG_PATH_INFO path = paths[i];
                        string gdiName = GetSourceName(path.sourceInfo);
                        if (String.IsNullOrWhiteSpace(gdiName))
                            continue;

                        DisplayInfo info = new DisplayInfo();
                        info.GdiName = gdiName;
                        info.AdapterLow = path.targetInfo.adapterId.LowPart;
                        info.AdapterHigh = path.targetInfo.adapterId.HighPart;
                        info.SourceId = path.sourceInfo.id;
                        info.TargetId = path.targetInfo.id;
                        info.AdvancedColorEnabled = GetAdvancedColorEnabled(path.targetInfo);
                        result.Add(info);
                    }

                    return result.ToArray();
                }
                finally
                {
                    Marshal.FreeHGlobal(modeBuffer);
                }
            }

            throw new InvalidOperationException("Display configuration kept changing while it was being queried.");
        }

        private static LUID ToLuid(DisplayInfo display)
        {
            LUID luid = new LUID();
            luid.LowPart = display.AdapterLow;
            luid.HighPart = display.AdapterHigh;
            return luid;
        }

        public static int GetSelectedScope(DisplayInfo display)
        {
            int scope;
            int hr = ColorProfileGetDisplayUserScope(ToLuid(display), display.SourceId, out scope);
            if (hr < 0)
                return WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER;
            return scope;
        }

        public static string GetDefaultProfile(DisplayInfo display, int scope, int subtype)
        {
            IntPtr ptr = IntPtr.Zero;
            int hr = ColorProfileGetDisplayDefault(
                scope,
                ToLuid(display),
                display.SourceId,
                CPT_ICC,
                subtype,
                out ptr);

            if (hr < 0 || ptr == IntPtr.Zero)
                return null;

            try
            {
                return Marshal.PtrToStringUni(ptr);
            }
            finally
            {
                LocalFree(ptr);
            }
        }

        public static string GetDefaultProfileWithFallback(DisplayInfo display, int subtype)
        {
            int selected = GetSelectedScope(display);
            string value = GetDefaultProfile(display, selected, subtype);
            if (!String.IsNullOrWhiteSpace(value))
                return value;

            int other = selected == WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER
                ? WCS_PROFILE_MANAGEMENT_SCOPE_SYSTEM_WIDE
                : WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER;

            return GetDefaultProfile(display, other, subtype);
        }

        public static int SetCurrentUserDefault(DisplayInfo display, int subtype, string profileName)
        {
            if (String.IsNullOrWhiteSpace(profileName))
                return 0;

            return ColorProfileSetDisplayDefaultAssociation(
                WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
                profileName,
                CPT_ICC,
                subtype,
                ToLuid(display),
                display.SourceId);
        }
    }
}
'@

if (-not ('ColorProfileWatchdog.Native' -as [type])) {
    Add-Type -TypeDefinition $nativeSource -Language CSharp
}

function Get-ActiveDisplays {
    return [ColorProfileWatchdog.Native]::GetActiveDisplays()
}

function Get-SavedProfileState {
    param($Display)

    $scope = [ColorProfileWatchdog.Native]::GetSelectedScope($Display)
    $standard = [ColorProfileWatchdog.Native]::GetDefaultProfileWithFallback(
        $Display,
        [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE
    )
    $extended = [ColorProfileWatchdog.Native]::GetDefaultProfileWithFallback(
        $Display,
        [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
    )

    [PSCustomObject]@{
        GdiName         = $Display.GdiName
        AdapterLow      = $Display.AdapterLow
        AdapterHigh     = $Display.AdapterHigh
        SourceId        = $Display.SourceId
        OriginalScope   = $scope
        StandardProfile = $standard
        ExtendedProfile = $extended
    }
}

function Restore-SavedProfiles {
    param(
        $CurrentDisplay,
        $SavedDisplay,
        [switch]$Force
    )

    if ($SavedDisplay.StandardProfile) {
        $current = [ColorProfileWatchdog.Native]::GetDefaultProfile(
            $CurrentDisplay,
            [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE
        )

        if ($Force -or $current -ne $SavedDisplay.StandardProfile) {
            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $CurrentDisplay,
                [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE,
                [string]$SavedDisplay.StandardProfile
            )
            if ($hr -lt 0) {
                Write-Log ('Failed to restore STANDARD profile on {0}: HRESULT 0x{1:X8}' -f $CurrentDisplay.GdiName, [uint32]$hr)
            } else {
                Write-Log ('Restored STANDARD profile on {0}: {1}' -f $CurrentDisplay.GdiName, $SavedDisplay.StandardProfile)
            }
        }
    }

    if ($SavedDisplay.ExtendedProfile) {
        $current = [ColorProfileWatchdog.Native]::GetDefaultProfile(
            $CurrentDisplay,
            [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
        )

        if ($Force -or $current -ne $SavedDisplay.ExtendedProfile) {
            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $CurrentDisplay,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE,
                [string]$SavedDisplay.ExtendedProfile
            )
            if ($hr -lt 0) {
                Write-Log ('Failed to restore EXTENDED profile on {0}: HRESULT 0x{1:X8}' -f $CurrentDisplay.GdiName, [uint32]$hr)
            } else {
                Write-Log ('Restored EXTENDED profile on {0}: {1}' -f $CurrentDisplay.GdiName, $SavedDisplay.ExtendedProfile)
            }
        }
    }
}

if ($Install) {
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Stop-ExistingWatchdog

    $build = [Environment]::OSVersion.Version.Build
    if ($build -lt 20348) {
        throw 'This standalone watchdog requires Windows build 20348 or newer.'
    }

    $displays = @(Get-ActiveDisplays)
    if ($displays.Count -eq 0) {
        throw 'No active displays were found.'
    }

    $saved = @()
    foreach ($display in $displays) {
        $saved += Get-SavedProfileState -Display $display
    }

    $state = [PSCustomObject]@{
        Version     = 1
        CapturedAt  = (Get-Date).ToString('o')
        Displays    = $saved
    }

    $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $StatePath -Encoding UTF8

    $escapedPs1 = $PSCommandPath.Replace('"', '""')
    $vbs = @"
Set shell = CreateObject("WScript.Shell")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$escapedPs1""", 0, False
"@
    Set-Content -LiteralPath $LauncherPath -Value $vbs -Encoding ASCII

    $runCommand = '"{0}" //B //Nologo "{1}"' -f (Join-Path $env:WINDIR 'System32\wscript.exe'), $LauncherPath
    New-Item -Path $RunKey -Force | Out-Null
    New-ItemProperty -Path $RunKey -Name $RunName -Value $runCommand -PropertyType String -Force | Out-Null

    Write-Host ''
    Write-Host 'Captured display profile associations:' -ForegroundColor Cyan
    foreach ($item in $saved) {
        Write-Host ('  {0}' -f $item.GdiName)
        Write-Host ('    SDR / STANDARD : {0}' -f $(if ($item.StandardProfile) { $item.StandardProfile } else { '<none - left untouched>' }))
        Write-Host ('    HDR / EXTENDED : {0}' -f $(if ($item.ExtendedProfile) { $item.ExtendedProfile } else { '<none - left untouched>' }))
    }

    Write-Host ''
    Write-Host 'Startup mode: hidden / invisible' -ForegroundColor Green
    Write-Host ('State: {0}' -f $StatePath)
    Write-Host ('Log  : {0}' -f $LogPath)

    Start-Process -FilePath (Join-Path $env:WINDIR 'System32\wscript.exe') `
        -ArgumentList @('//B', '//Nologo', ('"{0}"' -f $LauncherPath)) `
        -WindowStyle Hidden

    exit 0
}

$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\ColorProfileModeWatchdogStandalone', [ref]$createdNew)
if (-not $createdNew) {
    exit 0
}

try {
    if (-not (Test-Path -LiteralPath $StatePath)) {
        Write-Log 'State.json is missing; watchdog stopped.'
        exit 2
    }

    $state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
    if (-not $state.Displays) {
        Write-Log 'No saved display associations; watchdog stopped.'
        exit 3
    }

    Write-Log 'Watchdog started.'

    $lastModes = @{}
    $lastForced = [DateTime]::MinValue

    while ($true) {
        try {
            $currentDisplays = @(Get-ActiveDisplays)

            foreach ($current in $currentDisplays) {
                $saved = $state.Displays | Where-Object { $_.GdiName -eq $current.GdiName } | Select-Object -First 1
                if (-not $saved) {
                    continue
                }

                $modeKey = [string]$current.GdiName
                $modeNow = [bool]$current.AdvancedColorEnabled
                $modeChanged = $lastModes.ContainsKey($modeKey) -and ($lastModes[$modeKey] -ne $modeNow)
                $lastModes[$modeKey] = $modeNow

                if ($modeChanged) {
                    # Give Windows a moment to finish the Win+Alt+B transition,
                    # then explicitly reassert both independent associations.
                    Start-Sleep -Milliseconds 700
                    $refreshed = @(Get-ActiveDisplays | Where-Object { $_.GdiName -eq $current.GdiName } | Select-Object -First 1)
                    if ($refreshed.Count -gt 0) {
                        Restore-SavedProfiles -CurrentDisplay $refreshed[0] -SavedDisplay $saved -Force
                    }
                } else {
                    Restore-SavedProfiles -CurrentDisplay $current -SavedDisplay $saved
                }
            }

            # Fallback reassertion. This also covers systems where SDR/WCG and HDR
            # can both report Advanced Color enabled through the legacy query.
            if (((Get-Date) - $lastForced).TotalSeconds -ge 5.0) {
                foreach ($current in $currentDisplays) {
                    $saved = $state.Displays | Where-Object { $_.GdiName -eq $current.GdiName } | Select-Object -First 1
                    if ($saved) {
                        Restore-SavedProfiles -CurrentDisplay $current -SavedDisplay $saved -Force
                    }
                }
                $lastForced = Get-Date
            }
        } catch {
            Write-Log ('Loop error: ' + $_.Exception.Message)
        }

        Start-Sleep -Milliseconds 900
    }
}
finally {
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
}
