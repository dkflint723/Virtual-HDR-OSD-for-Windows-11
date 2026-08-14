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
$GammaStatePath = Join-Path $env:LOCALAPPDATA 'Virtual_HDR_OSD_for_Windows\gamma_hotkeys.json'
$LauncherPath = Join-Path $AppDir 'Launcher.vbs'
$TaskName = 'Virtual HDR OSD - Color Profile Mode Watchdog'

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
        public const int DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL = 11;

        public const int HOTKEY_OFF = 0x564801;
        public const int HOTKEY_ON = 0x564802;
        public const uint MOD_ALT = 0x0001;
        public const uint MOD_NOREPEAT = 0x4000;
        public const uint VK_1 = 0x31;
        public const uint VK_2 = 0x32;
        public const uint WM_HOTKEY = 0x0312;
        public const uint PM_REMOVE = 0x0001;

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

        [StructLayout(LayoutKind.Sequential)]
        public struct DISPLAYCONFIG_SDR_WHITE_LEVEL
        {
            public DISPLAYCONFIG_DEVICE_INFO_HEADER header;
            public UInt32 SDRWhiteLevel;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct POINT
        {
            public Int32 X;
            public Int32 Y;
        }

        [StructLayout(LayoutKind.Sequential)]
        public struct MSG
        {
            public IntPtr hwnd;
            public UInt32 message;
            public IntPtr wParam;
            public IntPtr lParam;
            public UInt32 time;
            public POINT pt;
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

        [DllImport("user32.dll", EntryPoint = "DisplayConfigGetDeviceInfo")]
        private static extern int DisplayConfigGetSdrWhiteLevel(
            ref DISPLAYCONFIG_SDR_WHITE_LEVEL requestPacket);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);

        [DllImport("user32.dll", SetLastError = true)]
        private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

        [DllImport("user32.dll")]
        private static extern int GetMessage(out MSG lpMsg, IntPtr hWnd, uint wMsgFilterMin, uint wMsgFilterMax);

        [DllImport("user32.dll")]
        private static extern bool PostThreadMessage(uint idThread, uint Msg, UIntPtr wParam, IntPtr lParam);

        [DllImport("kernel32.dll")]
        private static extern uint GetCurrentThreadId();

        private const uint WM_QUIT = 0x0012;
        private static readonly System.Collections.Concurrent.ConcurrentQueue<int> gammaHotkeyQueue =
            new System.Collections.Concurrent.ConcurrentQueue<int>();
        private static System.Threading.Thread gammaHotkeyThread;
        private static System.Threading.ManualResetEvent gammaHotkeyReady =
            new System.Threading.ManualResetEvent(false);
        private static volatile bool gammaHotkeysRegistered = false;
        private static uint gammaHotkeyThreadId = 0;

        private static void GammaHotkeyMessageLoop()
        {
            gammaHotkeyThreadId = GetCurrentThreadId();
            uint modifiers = MOD_ALT | MOD_NOREPEAT;
            bool off = RegisterHotKey(IntPtr.Zero, HOTKEY_OFF, modifiers, VK_1);
            bool on = RegisterHotKey(IntPtr.Zero, HOTKEY_ON, modifiers, VK_2);

            gammaHotkeysRegistered = off && on;
            if (!gammaHotkeysRegistered)
            {
                if (off) UnregisterHotKey(IntPtr.Zero, HOTKEY_OFF);
                if (on) UnregisterHotKey(IntPtr.Zero, HOTKEY_ON);
                gammaHotkeyReady.Set();
                return;
            }

            gammaHotkeyReady.Set();
            MSG msg;
            try
            {
                while (GetMessage(out msg, IntPtr.Zero, 0, 0) > 0)
                {
                    if (msg.message == WM_HOTKEY)
                    {
                        int id = msg.wParam.ToInt32();
                        if (id == HOTKEY_OFF || id == HOTKEY_ON)
                            gammaHotkeyQueue.Enqueue(id);
                    }
                }
            }
            finally
            {
                UnregisterHotKey(IntPtr.Zero, HOTKEY_OFF);
                UnregisterHotKey(IntPtr.Zero, HOTKEY_ON);
                gammaHotkeysRegistered = false;
                gammaHotkeyThreadId = 0;
            }
        }

        public static bool TryRegisterGammaHotkeys()
        {
            if (gammaHotkeysRegistered)
                return true;
            if (gammaHotkeyThread != null && gammaHotkeyThread.IsAlive)
                return false;

            gammaHotkeyReady.Reset();
            gammaHotkeyThread = new System.Threading.Thread(GammaHotkeyMessageLoop);
            gammaHotkeyThread.IsBackground = true;
            gammaHotkeyThread.Name = "ColorProfileWatchdog-Hotkeys";
            gammaHotkeyThread.Start();
            gammaHotkeyReady.WaitOne(1500);
            return gammaHotkeysRegistered;
        }

        public static int PollGammaHotkey()
        {
            int id;
            return gammaHotkeyQueue.TryDequeue(out id) ? id : 0;
        }

        public static void UnregisterGammaHotkeys()
        {
            uint threadId = gammaHotkeyThreadId;
            if (threadId != 0)
                PostThreadMessage(threadId, WM_QUIT, UIntPtr.Zero, IntPtr.Zero);
            if (gammaHotkeyThread != null && gammaHotkeyThread.IsAlive)
                gammaHotkeyThread.Join(1000);
            gammaHotkeysRegistered = false;
        }

        [DllImport("mscms.dll", CharSet = CharSet.Unicode)]
        private static extern bool GetColorDirectory(
            string pMachineName,
            System.Text.StringBuilder pBuffer,
            ref UInt32 pdwSize);

        public static string GetWindowsColorDirectory()
        {
            UInt32 size = 0;
            GetColorDirectory(null, null, ref size);
            if (size == 0)
                return System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32", "spool", "drivers", "color");

            System.Text.StringBuilder sb = new System.Text.StringBuilder((int)size);
            if (!GetColorDirectory(null, sb, ref size))
                return System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                    "System32", "spool", "drivers", "color");
            return sb.ToString();
        }

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

        public static double GetSdrWhiteLevelNits(DisplayInfo display)
        {
            DISPLAYCONFIG_SDR_WHITE_LEVEL packet = new DISPLAYCONFIG_SDR_WHITE_LEVEL();
            packet.header.type = DISPLAYCONFIG_DEVICE_INFO_GET_SDR_WHITE_LEVEL;
            packet.header.size = (UInt32)Marshal.SizeOf(typeof(DISPLAYCONFIG_SDR_WHITE_LEVEL));
            packet.header.adapterId = display.AdapterLuid;
            packet.header.id = display.TargetId;
            int rc = DisplayConfigGetSdrWhiteLevel(ref packet);
            if (rc != ERROR_SUCCESS)
                return 200.0;
            return ((double)packet.SDRWhiteLevel / 1000.0) * 80.0;
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

function Format-HResult {
    param([int]$Value)
    try {
        $bytes = [BitConverter]::GetBytes([int]$Value)
        $unsigned = [BitConverter]::ToUInt32($bytes, 0)
        return ('0x{0:X8}' -f $unsigned)
    } catch {
        return [string]$Value
    }
}

function Test-InstalledColorProfile {
    param([string]$ProfileName)
    if ([string]::IsNullOrWhiteSpace($ProfileName)) { return $false }
    try {
        $colorDir = [ColorProfileWatchdog.Native]::GetWindowsColorDirectory()
        return Test-Path -LiteralPath (Join-Path $colorDir $ProfileName)
    } catch {
        return $false
    }
}

function Resolve-StableWorkingPair {
    param(
        $Display,
        [string]$CurrentExtended,
        $GammaEntry
    )

    $off = $null
    $on = $null
    $enabled = $false

    # Highest-authority source: the profile that Windows is actually using now.
    # Stable Virtual HDR OSD working slots always use:
    #   Virtual_HDR_OSD_<display-token>_Off.icm
    #   Virtual_HDR_OSD_<display-token>_On.icm
    if ($CurrentExtended -match '^(Virtual_HDR_OSD_[A-Fa-f0-9]+)_(On|Off)\.icm$') {
        $prefix = $Matches[1]
        $slot = $Matches[2]
        $candidateOff = "${prefix}_Off.icm"
        $candidateOn = "${prefix}_On.icm"

        if (Test-InstalledColorProfile $candidateOff) { $off = $candidateOff }
        if (Test-InstalledColorProfile $candidateOn) { $on = $candidateOn }
        $enabled = ($slot -eq 'On')
    }

    # Runtime JSON is secondary only. Accept stable current-generation names and
    # verify that the files are actually installed before trusting them.
    if ($GammaEntry) {
        try {
            $candidate = [string]$GammaEntry.profiles.Off
            if (-not $off -and
                $candidate -match '^Virtual_HDR_OSD_[A-Fa-f0-9]+_Off\.icm$' -and
                (Test-InstalledColorProfile $candidate)) {
                $off = $candidate
            }
        } catch {}

        try {
            $candidate = [string]$GammaEntry.profiles.On
            if (-not $on -and
                $candidate -match '^Virtual_HDR_OSD_[A-Fa-f0-9]+_On\.icm$' -and
                (Test-InstalledColorProfile $candidate)) {
                $on = $candidate
            }
        } catch {}

        # Only use the JSON enable flag if the active Windows profile did not
        # already tell us which stable slot is selected.
        if ($CurrentExtended -notmatch '^Virtual_HDR_OSD_[A-Fa-f0-9]+_(On|Off)\.icm$') {
            try { $enabled = [bool]$GammaEntry.enabled } catch {}
        }
    }

    return [PSCustomObject]@{
        Off = $off
        On = $on
        Enabled = $enabled
    }
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

    $gammaEntry = Get-GammaEntryForDisplay -CurrentDisplay $Display
    $pair = Resolve-StableWorkingPair -Display $Display -CurrentExtended $extended -GammaEntry $gammaEntry

    [PSCustomObject]@{
        GdiName         = $Display.GdiName
        AdapterLow      = $Display.AdapterLow
        AdapterHigh     = $Display.AdapterHigh
        SourceId        = $Display.SourceId
        OriginalScope   = $scope
        StandardProfile = $standard
        ExtendedProfile = $extended
        WorkingOff      = $pair.Off
        WorkingOn       = $pair.On
        GammaEnabled    = [bool]$pair.Enabled
    }
}

function Get-GammaEntryForDisplay {
    param($CurrentDisplay)

    if (-not (Test-Path -LiteralPath $GammaStatePath)) { return $null }
    try {
        $gamma = Get-Content -Raw -LiteralPath $GammaStatePath | ConvertFrom-Json
        if (-not $gamma.displays) { return $null }
        foreach ($property in $gamma.displays.PSObject.Properties) {
            if ($property.Value.gdi_name -eq $CurrentDisplay.GdiName) {
                return $property.Value
            }
        }
    } catch {}
    return $null
}

function Get-DesiredExtendedProfile {
    param($CurrentDisplay, $SavedDisplay)

    # The standalone watchdog owns a persistent copy of the prepared Off/On names.
    # This works even when Virtual HDR OSD is closed and its runtime JSON is stale.
    if ($SavedDisplay.PSObject.Properties.Name -contains 'GammaEnabled') {
        if ([bool]$SavedDisplay.GammaEnabled -and $SavedDisplay.WorkingOn) {
            return [string]$SavedDisplay.WorkingOn
        }
        if (-not [bool]$SavedDisplay.GammaEnabled -and $SavedDisplay.WorkingOff) {
            return [string]$SavedDisplay.WorkingOff
        }
    }

    $entry = Get-GammaEntryForDisplay -CurrentDisplay $CurrentDisplay
    if ($entry -and $entry.active_profile) {
        return [string]$entry.active_profile
    }
    return [string]$SavedDisplay.ExtendedProfile
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
                Write-Log ('Failed to restore STANDARD profile on {0}: HRESULT {1}' -f $CurrentDisplay.GdiName, (Format-HResult $hr))
            } else {
                Write-Log ('Restored STANDARD profile on {0}: {1}' -f $CurrentDisplay.GdiName, $SavedDisplay.StandardProfile)
            }
        }
    }

    $desiredExtended = Get-DesiredExtendedProfile -CurrentDisplay $CurrentDisplay -SavedDisplay $SavedDisplay
    if ($desiredExtended) {
        $current = [ColorProfileWatchdog.Native]::GetDefaultProfile(
            $CurrentDisplay,
            [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
        )

        if ($Force -or $current -ne $desiredExtended) {
            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $CurrentDisplay,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE,
                [string]$desiredExtended
            )
            if ($hr -lt 0) {
                Write-Log ('Failed to restore EXTENDED profile on {0}: HRESULT {1}' -f $CurrentDisplay.GdiName, (Format-HResult $hr))
            } else {
                Write-Log ('Restored EXTENDED profile on {0}: {1}' -f $CurrentDisplay.GdiName, $desiredExtended)
            }
        }
    }
}

function Invoke-GammaHotkey {
    param([bool]$Enable)

    try {
        $currentDisplays = @(Get-ActiveDisplays)
        foreach ($current in $currentDisplays) {
            $saved = $state.Displays | Where-Object { $_.GdiName -eq $current.GdiName } | Select-Object -First 1
            if (-not $saved) { continue }

            $profile = $(if ($Enable) { $saved.WorkingOn } else { $saved.WorkingOff })
            if (-not $profile) {
                # Backward-compatible fallback if this watchdog was installed before the
                # self-contained state fields existed.
                $entry = Get-GammaEntryForDisplay -CurrentDisplay $current
                if ($entry) {
                    $profile = $(if ($Enable) { $entry.profiles.On } else { $entry.profiles.Off })
                }
            }
            if (-not $profile) {
                Write-Log ('Gamma hotkey ignored on {0}: stable {1} profile was not captured. Re-run the installer after Virtual HDR OSD has prepared both working profiles.' -f $current.GdiName, $(if($Enable){'ON'}else{'OFF'}))
                continue
            }

            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $current,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE,
                [string]$profile
            )
            if ($hr -lt 0) {
                Write-Log ('Gamma hotkey profile switch failed on {0}: HRESULT {1}' -f $current.GdiName, (Format-HResult $hr))
                continue
            }

            $saved.GammaEnabled = [bool]$Enable
            $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding UTF8

            # Keep the GUI runtime file synchronized when it exists, but do not depend on it.
            try {
                if (Test-Path -LiteralPath $GammaStatePath) {
                    $gamma = Get-Content -Raw -LiteralPath $GammaStatePath | ConvertFrom-Json
                    foreach ($property in $gamma.displays.PSObject.Properties) {
                        if ($property.Value.gdi_name -eq $current.GdiName) {
                            $property.Value.enabled = [bool]$Enable
                            $property.Value.active_profile = [string]$profile
                            $property.Value.updated_at = (Get-Date).ToString('o')
                        }
                    }
                    $gamma | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $GammaStatePath -Encoding UTF8
                }
            } catch {}

            $verify = [ColorProfileWatchdog.Native]::GetDefaultProfileWithFallback(
                $current,
                [ColorProfileWatchdog.Native]::CPST_EXTENDED_DISPLAY_COLOR_MODE
            )
            Write-Log ('Gamma correction {0} on {1}: requested={2}; readback={3}' -f $(if($Enable){'ON'}else{'OFF'}), $current.GdiName, $profile, $verify)
        }
    } catch {
        Write-Log ('Gamma hotkey error: ' + $_.Exception.Message)
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
        $item = Get-SavedProfileState -Display $display
        # If the GUI has recorded the real HDR base profile, preserve that as the
        # watchdog fallback instead of capturing an app-managed working profile.
        $gammaEntry = Get-GammaEntryForDisplay -CurrentDisplay $display
        if ($gammaEntry -and $gammaEntry.base_profile) {
            $baseCandidate = [string]$gammaEntry.base_profile
            if ($baseCandidate -and
                $baseCandidate -notmatch '^Virtual_HDR_OSD_' -and
                (Test-InstalledColorProfile $baseCandidate)) {
                $item.ExtendedProfile = $baseCandidate
            }
        }
        $saved += $item
    }

    foreach ($item in $saved) {
        $hasAnyWorking = [bool]$item.WorkingOff -or [bool]$item.WorkingOn
        if ($hasAnyWorking -and (-not $item.WorkingOff -or -not $item.WorkingOn)) {
            throw ('Virtual HDR OSD working pair is incomplete on {0}. Open Virtual HDR OSD, apply the current HDR state once so both Correction Off and Correction On exist, close the GUI, then run this installer again.' -f $item.GdiName)
        }
    }

    $state = [PSCustomObject]@{
        Version     = 2
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

    # Register persistence through the Task Scheduler COM API instead of the
    # ScheduledTasks CIM cmdlets.  A SID is unambiguous for local, Microsoft,
    # Entra ID, and domain-backed interactive accounts, and InteractiveToken
    # requires no stored password.  Keep HKCU Run only as a compatibility
    # fallback if Task Scheduler registration is unavailable on this machine.
    $startupMethod = 'Task Scheduler (COM / current-user SID)'
    $currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $currentSid = $currentIdentity.User.Value
    $wscriptPath = Join-Path $env:WINDIR 'System32\wscript.exe'
    $taskArguments = '//B //Nologo "{0}"' -f $LauncherPath

    try {
        $taskService = New-Object -ComObject 'Schedule.Service'
        $taskService.Connect()
        $taskRoot = $taskService.GetFolder('\')

        # Delete a previous definition first. This also repairs tasks created by
        # older builds with an incompatible principal/trigger identity.
        try { $taskRoot.DeleteTask($TaskName, 0) } catch {}

        $taskDefinition = $taskService.NewTask(0)
        $taskDefinition.RegistrationInfo.Description = 'Keeps Windows SDR/HDR color profile associations stable across display-mode changes.'
        $taskDefinition.Settings.Enabled = $true
        $taskDefinition.Settings.StartWhenAvailable = $true
        $taskDefinition.Settings.DisallowStartIfOnBatteries = $false
        $taskDefinition.Settings.StopIfGoingOnBatteries = $false
        $taskDefinition.Settings.MultipleInstances = 2  # TASK_INSTANCES_IGNORE_NEW

        # TASK_LOGON_INTERACTIVE_TOKEN = 3, TASK_RUNLEVEL_LUA = 0.
        $taskDefinition.Principal.UserId = $currentSid
        $taskDefinition.Principal.LogonType = 3
        $taskDefinition.Principal.RunLevel = 0

        # TASK_TRIGGER_LOGON = 9. LogonTrigger.UserId accepts a SID.
        $logonTrigger = $taskDefinition.Triggers.Create(9)
        $logonTrigger.UserId = $currentSid
        $logonTrigger.Delay = 'PT10S'
        $logonTrigger.Enabled = $true

        # TASK_ACTION_EXEC = 0.
        $execAction = $taskDefinition.Actions.Create(0)
        $execAction.Path = $wscriptPath
        $execAction.Arguments = $taskArguments

        # TASK_CREATE_OR_UPDATE = 6; TASK_LOGON_INTERACTIVE_TOKEN = 3.
        $taskRoot.RegisterTaskDefinition(
            $TaskName,
            $taskDefinition,
            6,
            $currentSid,
            $null,
            3,
            $null
        ) | Out-Null

        # Remove an old fallback entry if a previous installation needed it.
        Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Force -ErrorAction SilentlyContinue
    }
    catch {
        $startupMethod = 'HKCU Run fallback'
        Write-Warning ('Task Scheduler registration failed ({0}). Falling back to the current-user Run key.' -f $_.Exception.Message)
        $runCommand = '"{0}" //B //Nologo "{1}"' -f $wscriptPath, $LauncherPath
        New-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Force | Out-Null
        New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Value $runCommand -PropertyType String -Force | Out-Null
    }

    Write-Host ''
    Write-Host 'Captured display profile associations:' -ForegroundColor Cyan
    foreach ($item in $saved) {
        Write-Host ('  {0}' -f $item.GdiName)
        Write-Host ('    SDR / STANDARD : {0}' -f $(if ($item.StandardProfile) { $item.StandardProfile } else { '<none - left untouched>' }))
        Write-Host ('    HDR / EXTENDED : {0}' -f $(if ($item.ExtendedProfile) { $item.ExtendedProfile } else { '<none - left untouched>' }))
        Write-Host ('    Gamma OFF      : {0}' -f $(if ($item.WorkingOff) { $item.WorkingOff } else { '<not prepared by Virtual HDR OSD>' }))
        Write-Host ('    Gamma ON       : {0}' -f $(if ($item.WorkingOn) { $item.WorkingOn } else { '<not prepared by Virtual HDR OSD>' }))
    }

    Write-Host ''
    Write-Host ('Startup mode: {0} / hidden / 10-second Task Scheduler delay when supported' -f $startupMethod) -ForegroundColor Green
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
    $hotkeysRegistered = [ColorProfileWatchdog.Native]::TryRegisterGammaHotkeys()
    $lastHotkeyRetry = Get-Date
    Write-Log $(if ($hotkeysRegistered) { 'Global hotkey thread active: Alt+1 OFF, Alt+2 ON.' } else { 'Global hotkey thread unavailable; registration will be retried.' })

    $pollCounter = 0
    while ($true) {
        if (-not $hotkeysRegistered -and ((Get-Date) - $lastHotkeyRetry).TotalSeconds -ge 2.0) {
            $hotkeysRegistered = [ColorProfileWatchdog.Native]::TryRegisterGammaHotkeys()
            $lastHotkeyRetry = Get-Date
            if ($hotkeysRegistered) { Write-Log 'Global hotkey thread started after retry: Alt+1 OFF, Alt+2 ON.' }
        }
        if ($hotkeysRegistered) {
            while ($true) {
                $hotkey = [ColorProfileWatchdog.Native]::PollGammaHotkey()
                if ($hotkey -eq 0) { break }
                if ($hotkey -eq [ColorProfileWatchdog.Native]::HOTKEY_OFF) { Invoke-GammaHotkey -Enable $false }
                elseif ($hotkey -eq [ColorProfileWatchdog.Native]::HOTKEY_ON) { Invoke-GammaHotkey -Enable $true }
            }
        }
        $pollCounter++
        if (($pollCounter % 8) -ne 0) {
            Start-Sleep -Milliseconds 100
            continue
        }

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

        Start-Sleep -Milliseconds 100
    }
}
finally {
    try { [ColorProfileWatchdog.Native]::UnregisterGammaHotkeys() } catch {}
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch {}
        $mutex.Dispose()
    }
}
