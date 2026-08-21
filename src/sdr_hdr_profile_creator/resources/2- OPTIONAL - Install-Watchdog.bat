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

rem The write is verified by reading it back and comparing, not by trusting an exit code.
rem A failed Set-Content is a non-terminating error, so powershell.exe still exits 0 and
rem "if errorlevel 1" never fires. The old check then read the file back and confirmed it
rem looked like a watchdog -- which a stale copy from a previous install does. The result
rem was an installer that reported success while leaving the previous version in place,
rem silently, every time something blocked the write.
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; try { $raw = Get-Content -Raw -LiteralPath $env:INSTALL_BAT; $marker=':__WATCHDOG_POWERSHELL_PAYLOAD__'; $i=$raw.LastIndexOf($marker); if($i -lt 0){throw 'Embedded watchdog payload was not found.'}; $payload=$raw.Substring($i+$marker.Length).TrimStart([char]13,[char]10); Set-Content -LiteralPath $env:WATCHDOG_PATH -Value $payload -Encoding UTF8; $back = Get-Content -Raw -LiteralPath $env:WATCHDOG_PATH; if($back.Trim() -ne $payload.Trim()){ throw ('Wrote ' + $env:WATCHDOG_PATH + ' but read back different content.') } } catch { Write-Host ''; Write-Host ('  ' + $_.Exception.Message); exit 1 }" ^
  1>nul
if errorlevel 1 (
    echo.
    echo ERROR: Could not write the watchdog to:
    echo   %WATCHDOG%
    echo.
    echo The previous version, if any, has been left untouched.
    echo A security product blocking writes to AppData is the usual cause:
    echo check Controlled Folder Access under Windows Security, Ransomware protection.
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
# Written at the very end of a successful -Install and read by the GUI, which cannot
# see this console. Deleted first, so a stale file from a previous run cannot be
# mistaken for this one's result.
$ResultPath = Join-Path $AppDir 'install_result.json'
$script:InstallWarnings = @()
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
    # Two kinds of process, and the order matters. Launcher.vbs runs under
    # wscript.exe and restarts the watchdog five seconds after it exits, so killing
    # only the PowerShell -- which is all this used to do, because the filter named
    # powershell.exe and pwsh.exe and nothing else -- just got it started again. The
    # supervisor goes first, then its child.
    try {
        $launcher = [Regex]::Escape($LauncherPath)
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object {
                ($_.Name -ieq 'wscript.exe' -or $_.Name -ieq 'cscript.exe') -and
                $_.CommandLine -and
                ($_.CommandLine -match $launcher) -and
                ($_.ProcessId -ne $PID)
            } |
            ForEach-Object {
                Invoke-CimMethod -InputObject $_ -MethodName Terminate -ErrorAction SilentlyContinue | Out-Null
            }
    } catch {}

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

        // Startup guard. The watchdog has been observed acquiring the singleton
        // mutex and then blocking before its main loop ever begins, which leaves a
        // process that enforces nothing while preventing any healthy instance from
        // starting. If startup does not complete in time, say so in the log and exit
        // so the next instance can take over. Implemented here rather than as a
        // PowerShell timer because a scriptblock delegate invoked on a threadpool
        // thread has no runspace and would not run.
        private static volatile bool _startupComplete = false;
        private static long _lastAliveTicks = 0;

        public static void MarkStartupComplete()
        {
            _startupComplete = true;
            MarkAlive();
        }

        // Called once per reconcile pass. The loop has been seen to stop making
        // progress after a reboot without throwing, logging, or exiting, which
        // leaves a process that holds the singleton and enforces nothing.
        public static void MarkAlive()
        {
            System.Threading.Interlocked.Exchange(ref _lastAliveTicks, DateTime.UtcNow.Ticks);
        }

        public static void ArmStartupGuard(string logPath, int seconds, int stallSeconds)
        {
            System.Threading.Thread guard = new System.Threading.Thread(delegate()
            {
                DateTime deadline = DateTime.UtcNow.AddSeconds(seconds);
                string reason = null;
                while (true)
                {
                    System.Threading.Thread.Sleep(200);
                    if (!_startupComplete)
                    {
                        if (DateTime.UtcNow >= deadline)
                        {
                            reason = "Startup did not finish within " + seconds + "s";
                            break;
                        }
                        continue;
                    }
                    long last = System.Threading.Interlocked.Read(ref _lastAliveTicks);
                    double stalled = (DateTime.UtcNow - new DateTime(last, DateTimeKind.Utc)).TotalSeconds;
                    if (stalled > stallSeconds)
                    {
                        reason = "Reconcile loop stalled for " + ((int)stalled) + "s";
                        break;
                    }
                }
                try
                {
                    System.IO.File.AppendAllText(
                        logPath,
                        DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss.fff")
                            + "  " + reason + "; exiting so a fresh instance can start."
                            + Environment.NewLine);
                }
                catch { }
                Environment.Exit(9);
            });
            guard.IsBackground = true;
            guard.Start();
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

function Resolve-BaseExtendedProfile {
    param($GammaEntry, [string]$CurrentExtended)

    # The captured HDR fallback must be the user's own profile, never one of Virtual
    # HDR OSD's generated working profiles: adopting one makes the watchdog restore
    # already-edited data as though it were the source.
    #
    # The GUI publishes both a name and a full path. Older builds wrote the ICC
    # description into the name field, which is not a filename at all -- Windows HDR
    # Calibration describes a profile with slashes in it -- so the path is tried as
    # well before giving up.
    $candidates = @()
    if ($GammaEntry) {
        if ($GammaEntry.PSObject.Properties.Name -contains 'base_profile') {
            $candidates += [string]$GammaEntry.base_profile
        }
        if ($GammaEntry.PSObject.Properties.Name -contains 'base_profile_path') {
            $raw = [string]$GammaEntry.base_profile_path
            if (-not [string]::IsNullOrWhiteSpace($raw)) {
                try { $candidates += [System.IO.Path]::GetFileName($raw) } catch {}
            }
        }
    }
    foreach ($candidate in $candidates) {
        if ([string]::IsNullOrWhiteSpace($candidate)) { continue }
        if ($candidate -match '^Virtual_HDR_OSD_') { continue }
        if (Test-InstalledColorProfile $candidate) { return $candidate }
    }

    # Nothing usable. Keep the current association only when it is not ours; an
    # app-managed name here would be a circular fallback, so report none instead.
    if ($CurrentExtended -match '^Virtual_HDR_OSD_') { return '' }
    return $CurrentExtended
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
        # Install captures the associations as they are right now, so this capture is by
        # definition the most recent decision. Anything the GUI does afterwards is newer
        # and takes over; see Get-DesiredExtendedProfile.
        GammaUpdatedAt  = (Get-Date).ToString('o')
    }
}

function Get-GammaEntryForDisplay {
    param($CurrentDisplay)

    if (-not (Test-Path -LiteralPath $GammaStatePath)) { return $null }
    try {
        # The GUI publishes this file by writing a temporary copy and renaming over
        # the original, so a read landing in that window fails. Returning $null then
        # makes the caller fall back to the state captured at install time, which can
        # assert the OPPOSITE correction variant -- the user's choice appears to
        # revert seconds after they make it, with nothing logged. Retry briefly.
        $gamma = $null
        foreach ($attempt in 1..4) {
            try {
                $raw = Get-Content -Raw -LiteralPath $GammaStatePath -ErrorAction Stop
                if (-not [string]::IsNullOrWhiteSpace($raw)) {
                    $gamma = $raw | ConvertFrom-Json
                    break
                }
            } catch {}
            Start-Sleep -Milliseconds (40 * $attempt)
        }
        if (-not $gamma) { return $null }
        if (-not $gamma.displays) { return $null }

        # Records are keyed by an id derived from the adapter LUID, which Windows
        # reissues on reboots and driver restarts, so one monitor accumulates several
        # records carrying the same gdi_name. Returning the first match handed the
        # caller a stale record naming profiles that no longer exist, defeating the
        # intent comparison in Get-DesiredExtendedProfile. Take the newest record.
        $candidates = @()
        foreach ($property in $gamma.displays.PSObject.Properties) {
            if ($property.Value.gdi_name -eq $CurrentDisplay.GdiName) {
                $candidates += $property.Value
            }
        }
        if ($candidates.Count -eq 0) { return $null }
        if ($candidates.Count -eq 1) { return $candidates[0] }

        # When no timestamp is usable, fall back to the last record written.
        $best = $candidates[$candidates.Count - 1]
        $bestAt = $null
        foreach ($candidate in $candidates) {
            $at = ConvertTo-GammaTimestamp ([string]$candidate.updated_at)
            if ($null -eq $at) { continue }
            if (($null -eq $bestAt) -or ($at -gt $bestAt)) {
                $best = $candidate
                $bestAt = $at
            }
        }
        return $best
    } catch {}
    return $null
}

function ConvertTo-GammaTimestamp {
    param([string]$Value)

    # Both sides have written slightly different ISO-8601 shapes: the GUI wrote a naive
    # local time for a long while, this script writes a round-trip value with an offset.
    # AssumeLocal makes the naive form comparable. A malformed or empty value must return
    # $null rather than throw, because $ErrorActionPreference is 'Stop' here.
    if ([string]::IsNullOrWhiteSpace($Value)) { return $null }
    $parsed = [datetimeoffset]::MinValue
    if ([datetimeoffset]::TryParse(
            $Value,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::AssumeLocal,
            [ref]$parsed)) {
        return $parsed
    }
    return $null
}

function Get-DesiredExtendedProfile {
    param($CurrentDisplay, $SavedDisplay)

    $entry = Get-GammaEntryForDisplay -CurrentDisplay $CurrentDisplay

    # Whichever side acted most recently wins.
    #
    # Virtual HDR OSD records its own correction changes in gamma_hotkeys.json; this
    # script records the switches it performs itself in State.json. Previously only the
    # captured GammaEnabled below was consulted, so a correction change made in the GUI
    # was re-associated back to the opposite variant by the next forced restore, roughly
    # every five seconds, with no way for the user to make it stick.
    if ($entry -and ($entry.PSObject.Properties.Name -contains 'enabled')) {
        $guiAt = ConvertTo-GammaTimestamp ([string]$entry.updated_at)
        $ownAt = $null
        if ($SavedDisplay.PSObject.Properties.Name -contains 'GammaUpdatedAt') {
            $ownAt = ConvertTo-GammaTimestamp ([string]$SavedDisplay.GammaUpdatedAt)
        }

        if ($guiAt -and ((-not $ownAt) -or ($guiAt -gt $ownAt))) {
            $wanted = $(if ([bool]$entry.enabled) { 'On' } else { 'Off' })

            # Prefer the filenames the GUI last published. It regenerates the working pair
            # under new names whenever the adapter LUID changes, which State.json captured
            # at install time cannot know about.
            $name = $null
            if (($entry.PSObject.Properties.Name -contains 'profiles') -and $entry.profiles) {
                if ($entry.profiles.PSObject.Properties.Name -contains $wanted) {
                    $name = [string]$entry.profiles.$wanted
                }
            }
            if ([string]::IsNullOrWhiteSpace($name)) {
                $name = [string]$(if ($wanted -eq 'On') { $SavedDisplay.WorkingOn } else { $SavedDisplay.WorkingOff })
            }

            # Never hand Windows a profile that is not installed.
            if ((-not [string]::IsNullOrWhiteSpace($name)) -and (Test-InstalledColorProfile -ProfileName $name)) {
                return $name
            }

            # The requested variant is unusable. Falling through to the captured state
            # here would answer with the OPPOSITE variant whenever GammaEnabled still
            # disagrees -- turning the correction back ON moments after the user chose
            # Off, which is precisely the guarantee this block exists to keep. Try the
            # captured name for the SAME direction, and otherwise change nothing.
            $sameDirection = [string]$(if ($wanted -eq 'On') { $SavedDisplay.WorkingOn } else { $SavedDisplay.WorkingOff })
            if ((-not [string]::IsNullOrWhiteSpace($sameDirection)) -and
                (Test-InstalledColorProfile -ProfileName $sameDirection)) {
                return $sameDirection
            }
            Write-Log ('Gamma correction {0} was requested for {1} but no installed profile provides it; leaving the association unchanged.' -f $wanted.ToUpper(), $CurrentDisplay.GdiName)
            return ''
        }
    }

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

    # The forced reassertion below runs every five seconds whether or not anything
    # drifted, so leaving SDR alone has to be decided here. Virtual HDR OSD offers
    # "third-party calibration owns SDR" for exactly this -- Calman and friends
    # reload the STANDARD association themselves, and a redundant write on a timer
    # is what fights them. The GUI publishes the choice; honour it.
    $sdrUnmanaged = $false
    # What to reassert, in order of authority: a profile the user pinned in the GUI,
    # otherwise whatever was associated when this watchdog was installed.
    #
    # Only the boolean used to be published, so the force-write below always used the
    # install-time capture. A pin the GUI had just reported as "restored" was reverted
    # within five seconds, and the log said so in the user's own words -- "Restored
    # STANDARD profile" -- naming the profile they had just replaced. Re-running the
    # installer did not help either: it re-captures the live association, which by then
    # is the reverted one.
    $sdrDesired = [string]$SavedDisplay.StandardProfile
    $sdrEntry = Get-GammaEntryForDisplay -CurrentDisplay $CurrentDisplay
    if ($sdrEntry) {
        if ($sdrEntry.PSObject.Properties.Name -contains 'sdr_unmanaged') {
            $sdrUnmanaged = [bool]$sdrEntry.sdr_unmanaged
        }
        if ($sdrEntry.PSObject.Properties.Name -contains 'sdr_profile') {
            $sdrPinned = [string]$sdrEntry.sdr_profile
            if ($sdrPinned) { $sdrDesired = $sdrPinned }
        }
    }

    if ($sdrDesired -and (-not $sdrUnmanaged)) {
        $current = [ColorProfileWatchdog.Native]::GetDefaultProfile(
            $CurrentDisplay,
            [ColorProfileWatchdog.Native]::WCS_PROFILE_MANAGEMENT_SCOPE_CURRENT_USER,
            [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE
        )

        if ($Force -or $current -ne $sdrDesired) {
            $hr = [ColorProfileWatchdog.Native]::SetCurrentUserDefault(
                $CurrentDisplay,
                [ColorProfileWatchdog.Native]::CPST_STANDARD_DISPLAY_COLOR_MODE,
                $sdrDesired
            )
            if ($hr -lt 0) {
                Write-Log ('Failed to restore STANDARD profile on {0}: HRESULT {1}' -f $CurrentDisplay.GdiName, (Format-HResult $hr))
            } else {
                Write-Log ('Restored STANDARD profile on {0}: {1}' -f $CurrentDisplay.GdiName, $sdrDesired)
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
            # ONE stamp, written to both files. Two Get-Date calls left the runtime file
            # microseconds newer than State.json, so the comparison in
            # Get-DesiredExtendedProfile always favoured the runtime copy and this
            # script's own captured state could never win a tie.
            $switchedAt = [DateTimeOffset]::Now.ToString('o', [System.Globalization.CultureInfo]::InvariantCulture)
            # Add-Member -Force also creates the property on State.json files written
            # before this field existed; a plain assignment throws on those.
            $saved | Add-Member -NotePropertyName GammaUpdatedAt -NotePropertyValue $switchedAt -Force
            $state | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $StatePath -Encoding UTF8

            # Keep the GUI runtime file synchronized when it exists, but do not depend on it.
            try {
                if (Test-Path -LiteralPath $GammaStatePath) {
                    $gamma = Get-Content -Raw -LiteralPath $GammaStatePath | ConvertFrom-Json
                    foreach ($property in $gamma.displays.PSObject.Properties) {
                        if ($property.Value.gdi_name -eq $current.GdiName) {
                            $property.Value.enabled = [bool]$Enable
                            $property.Value.active_profile = [string]$profile
                            $property.Value.updated_at = $switchedAt
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
    # A result file left by a previous run must never be mistaken for this one's.
    Remove-Item -LiteralPath $ResultPath -Force -ErrorAction SilentlyContinue

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
        $gammaEntry = Get-GammaEntryForDisplay -CurrentDisplay $display
        $item.ExtendedProfile = Resolve-BaseExtendedProfile -GammaEntry $gammaEntry -CurrentExtended $item.ExtendedProfile
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
    # The launcher supervises rather than fire-and-forget. The watchdog exits itself
    # when its guard sees startup or the reconcile loop stall, and without a
    # supervisor that would simply leave no watchdog running until the next logon.
    # Run with bWaitOnReturn = True so this script blocks until the process ends,
    # then restart it after a short pause.
    # bWaitOnReturn = True makes Run return the watchdog's exit code, which is the
    # only way this loop can tell "restart me" from "you are surplus". Without that
    # distinction a second supervisor -- one arrives whenever both the scheduled task
    # and the Run key are armed -- respawned a PowerShell that lost the singleton and
    # exited immediately, every five seconds, for as long as the session lasted.
    #   4  another instance already holds the singleton  -> stand down
    #   2  no saved state    3  no saved displays        -> will not fix itself
    #   9  the startup guard asked for a fresh instance  -> restart, that is the point
    $vbs = @"
Set shell = CreateObject("WScript.Shell")
Do
  code = shell.Run("powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""$escapedPs1""", 0, True)
  If code = 4 Or code = 2 Or code = 3 Then
    WScript.Quit 0
  End If
  WScript.Sleep 5000
Loop
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
        $registrationError = $_.Exception.Message

        # Arming the Run key without checking is what left both mechanisms live at
        # once. The DeleteTask above is usually what failed -- an earlier elevated
        # install owns the task and this account cannot remove it -- so the old task
        # survived and the Run key was added beside it. Two supervisors then start at
        # every sign-in, one loses the singleton, and it respawns forever.
        $taskSurvives = $false
        try {
            $probe = New-Object -ComObject 'Schedule.Service'
            $probe.Connect()
            $probeRoot = $probe.GetFolder('\')
            try { $probeRoot.DeleteTask($TaskName, 0) } catch {}
            try { $probeRoot.GetTask($TaskName) | Out-Null; $taskSurvives = $true } catch { $taskSurvives = $false }
        } catch { $taskSurvives = $false }

        if ($taskSurvives) {
            $startupMethod = 'existing scheduled task (kept; Run key deliberately not added)'
            $script:InstallWarnings += 'The scheduled task from an earlier elevated install could not be replaced by this account. It was left running and the startup entry was not duplicated. To replace it, press Run as Admin and install again.'
            Write-Warning ('Task Scheduler registration failed ({0}).' -f $registrationError)
            Write-Warning 'The existing task could not be removed either, so it was created by an elevated install and this account cannot replace it.'
            Write-Warning 'It is left in place and the Run key is NOT being added: arming both starts two watchdogs at every sign-in, and the surplus one spins.'
            Write-Warning 'To replace the task, press Run as Admin in Virtual HDR OSD, or right-click this installer and choose Run as administrator.'
            Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Force -ErrorAction SilentlyContinue
        } else {
            $startupMethod = 'HKCU Run fallback'
            $script:InstallWarnings += 'Windows refused to register the scheduled task, so a plain startup entry was used instead. The watchdog will start at sign-in without the ten-second delay that lets the display stack settle first.'
            Write-Warning ('Task Scheduler registration failed ({0}). Falling back to the current-user Run key.' -f $registrationError)
            $runCommand = '"{0}" //B //Nologo "{1}"' -f $wscriptPath, $LauncherPath
            New-Item -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Force | Out-Null
            New-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'ColorProfileModeWatchdog' -Value $runCommand -PropertyType String -Force | Out-Null
        }
    }

    Write-Host ''
    Write-Host 'What the watchdog will keep in place:' -ForegroundColor Cyan
    foreach ($item in $saved) {
        Write-Host ('  {0}' -f $item.GdiName)
        Write-Host ('    SDR / STANDARD : {0}' -f $(if ($item.StandardProfile) { $item.StandardProfile } else { '<none - left untouched>' }))
        # An empty ExtendedProfile does NOT mean HDR is unprotected. It means the live
        # association is this app's own working profile, which Resolve-BaseExtendedProfile
        # deliberately refuses to adopt as a fallback -- otherwise the watchdog would
        # restore already-edited output as its own source. The pair below is what it
        # actually reasserts, with -Force, every five seconds. Reporting that case as
        # "left untouched" said the opposite of what happens, and contradicted the app's
        # own status bar at the same moment.
        Write-Host ('    HDR / EXTENDED : {0}' -f $(if ($item.ExtendedProfile) { $item.ExtendedProfile } else { '<managed by the Gamma OFF/ON pair below>' }))
        Write-Host ('    Gamma OFF      : {0}' -f $(if ($item.WorkingOff) { $item.WorkingOff } else { '<not prepared by Virtual HDR OSD>' }))
        Write-Host ('    Gamma ON       : {0}' -f $(if ($item.WorkingOn) { $item.WorkingOn } else { '<not prepared by Virtual HDR OSD>' }))
    }

    # The GUI used to decide "installed" from Watchdog.ps1's mtime, which the .bat
    # writes before the integrity check, before -Install, before the display capture
    # and before Task Scheduler registration. So every later failure -- including an
    # outright throw -- was still reported as a green "Watchdog installed."
    # Write down what actually happened instead, for the GUI to read.
    $result = [PSCustomObject]@{
        action   = 'install'
        ok       = $true
        startup  = $startupMethod
        fallback = ($startupMethod -notlike 'Task Scheduler*')
        warnings = @($script:InstallWarnings)
        at       = (Get-Date).ToString('o')
    }
    try {
        $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
    } catch {}

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
    # 4, not 0: Launcher.vbs reads this to decide whether it is the surplus supervisor
    # and should stand down. 0 is indistinguishable from an ordinary exit, and the
    # loop treated it as "restart me".
    exit 4
}

try {
    # Each step is logged so that a hang names its own location; previously the
    # first log line came after all of this, so a stuck instance was silent.
    Write-Log 'Startup: singleton acquired.'
    [ColorProfileWatchdog.Native]::ArmStartupGuard($LogPath, 25, 60)

    if (-not (Test-Path -LiteralPath $StatePath)) {
        Write-Log 'State.json is missing; watchdog stopped.'
        exit 2
    }

    $state = Get-Content -Raw -LiteralPath $StatePath | ConvertFrom-Json
    if (-not $state.Displays) {
        Write-Log 'No saved display associations; watchdog stopped.'
        exit 3
    }
    Write-Log 'Startup: saved state loaded.'

    Write-Log 'Watchdog started.'

    $lastModes = @{}
    $lastForced = [DateTime]::MinValue
    $hotkeysRegistered = [ColorProfileWatchdog.Native]::TryRegisterGammaHotkeys()
    $lastHotkeyRetry = Get-Date
    Write-Log $(if ($hotkeysRegistered) { 'Global hotkey thread active: Alt+1 OFF, Alt+2 ON.' } else { 'Global hotkey thread unavailable; registration will be retried.' })

    # Past every step that has been seen to block; disarm the guard.
    [ColorProfileWatchdog.Native]::MarkStartupComplete()

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
        [ColorProfileWatchdog.Native]::MarkAlive()

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
